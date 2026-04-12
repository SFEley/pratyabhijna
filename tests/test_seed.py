"""Tests for the ``seed`` module.

TDD: these tests define the seed_subject contract. The seed command
populates the subject Person node's soul and identity tiers from
prose files chosen by the deployment. It is a deliberate CLI action,
not an MCP tool — soul and identity are protected from automated
modification.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_subject_node


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """A mock PratyabhijnaService for seed tests."""
    service = MagicMock()
    service.config = MagicMock()
    service.config.subject_name = "TestSubject"
    service.get_entity_by_name = AsyncMock(return_value=None)
    service.start = AsyncMock()
    service.stop = AsyncMock()

    # Mock graphiti internals used by seeder
    service._graphiti = MagicMock()
    service._graphiti.embedder = MagicMock()
    service._graphiti.driver = MagicMock()
    return service


@pytest.fixture
def identity_files(tmp_path):
    """Create temporary soul and identity files."""
    soul_path = tmp_path / "SOUL.md"
    identity_path = tmp_path / "IDENTITY.md"
    soul_path.write_text("Soul placeholder for tests.")
    identity_path.write_text("Identity placeholder for tests.")
    return soul_path, identity_path


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------

class TestSeedReadsFiles:
    async def test_reads_soul_and_identity(self, mock_service, identity_files):
        """seed_subject reads content from the given file paths."""
        from pratyabhijna.seed import seed_subject

        soul_path, identity_path = identity_files

        with patch("pratyabhijna.seed._create_subject_node", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = MagicMock()
            result = await seed_subject(
                mock_service, soul_path=soul_path, identity_path=identity_path,
            )

        assert result["soul_loaded"] is True
        assert result["identity_loaded"] is True

    async def test_handles_missing_soul_file(self, mock_service, tmp_path):
        """Warns but doesn't crash when SOUL.md doesn't exist."""
        from pratyabhijna.seed import seed_subject

        identity_path = tmp_path / "IDENTITY.md"
        identity_path.write_text("I orient toward thresholds.")
        missing_soul = tmp_path / "SOUL.md"  # does not exist

        with patch("pratyabhijna.seed._create_subject_node", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = MagicMock()
            result = await seed_subject(
                mock_service, soul_path=missing_soul, identity_path=identity_path,
            )

        assert result["soul_loaded"] is False
        assert result["identity_loaded"] is True

    async def test_handles_missing_identity_file(self, mock_service, tmp_path):
        """Warns but doesn't crash when IDENTITY.md doesn't exist."""
        from pratyabhijna.seed import seed_subject

        soul_path = tmp_path / "SOUL.md"
        soul_path.write_text("Soul placeholder.")
        missing_identity = tmp_path / "IDENTITY.md"  # does not exist

        with patch("pratyabhijna.seed._create_subject_node", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = MagicMock()
            result = await seed_subject(
                mock_service, soul_path=soul_path, identity_path=missing_identity,
            )

        assert result["soul_loaded"] is True
        assert result["identity_loaded"] is False


# ---------------------------------------------------------------------------
# Creating new node
# ---------------------------------------------------------------------------

class TestSeedCreatesNode:
    async def test_creates_person_node_when_missing(self, mock_service, identity_files):
        """When no subject node exists, creates one with soul and identity."""
        from pratyabhijna.seed import seed_subject

        soul_path, identity_path = identity_files
        mock_service.get_entity_by_name.return_value = None

        with patch("pratyabhijna.seed.EntityNode") as MockNode:
            instance = MagicMock()
            instance.generate_name_embedding = AsyncMock()
            instance.save = AsyncMock()
            MockNode.return_value = instance

            result = await seed_subject(
                mock_service, soul_path=soul_path, identity_path=identity_path,
            )

        assert result["action"] == "created"
        instance.generate_name_embedding.assert_called_once()
        instance.save.assert_called_once()

    async def test_new_node_has_correct_attributes(self, mock_service, identity_files):
        """New node has person_type, soul, and identity attributes."""
        from pratyabhijna.seed import seed_subject

        soul_path, identity_path = identity_files
        mock_service.get_entity_by_name.return_value = None

        created_attrs = {}
        with patch("pratyabhijna.seed.EntityNode") as MockNode:
            instance = MagicMock()
            instance.generate_name_embedding = AsyncMock()
            instance.save = AsyncMock()

            def capture_init(**kwargs):
                created_attrs.update(kwargs.get("attributes", {}))
                return instance
            MockNode.side_effect = capture_init

            await seed_subject(
                mock_service, soul_path=soul_path, identity_path=identity_path,
            )

        assert created_attrs["person_type"] == "AI"
        assert created_attrs["soul"] == "Soul placeholder for tests."
        assert created_attrs["identity"] == "Identity placeholder for tests."


# ---------------------------------------------------------------------------
# Updating existing node
# ---------------------------------------------------------------------------

class TestSeedUpdatesNode:
    async def test_updates_existing_node(self, mock_service, identity_files):
        """When subject node exists, updates soul and identity attributes."""
        from pratyabhijna.seed import seed_subject

        soul_path, identity_path = identity_files
        existing = make_subject_node(
            soul="Old soul.",
            identity="Old identity.",
            context="Existing context.",
            context_rebuilt_at=None,
        )
        mock_service.get_entity_by_name.return_value = existing

        with patch("pratyabhijna.seed.EntityNode.save", new_callable=AsyncMock):
            result = await seed_subject(
                mock_service, soul_path=soul_path, identity_path=identity_path,
            )

        assert result["action"] == "updated"
        assert existing.attributes["soul"] == "Soul placeholder for tests."
        assert existing.attributes["identity"] == "Identity placeholder for tests."

    async def test_preserves_context_on_update(self, mock_service, identity_files):
        """Seeding never overwrites the context or context_rebuilt_at."""
        from pratyabhijna.seed import seed_subject

        soul_path, identity_path = identity_files
        existing = make_subject_node(
            soul="Old soul.",
            identity="Old identity.",
            context="Precious existing context.",
            context_rebuilt_at=None,
        )
        mock_service.get_entity_by_name.return_value = existing

        with patch("pratyabhijna.seed.EntityNode.save", new_callable=AsyncMock):
            await seed_subject(
                mock_service, soul_path=soul_path, identity_path=identity_path,
            )

        assert existing.attributes["context"] == "Precious existing context."


# ---------------------------------------------------------------------------
# Return value
# ---------------------------------------------------------------------------

class TestSeedReturn:
    async def test_returns_summary(self, mock_service, identity_files):
        """seed_subject returns a summary dict with action and load status."""
        from pratyabhijna.seed import seed_subject

        soul_path, identity_path = identity_files

        with patch("pratyabhijna.seed._create_subject_node", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = MagicMock()
            result = await seed_subject(
                mock_service, soul_path=soul_path, identity_path=identity_path,
            )

        assert "action" in result
        assert "soul_loaded" in result
        assert "identity_loaded" in result
        assert "subject" in result
        assert result["subject"] == "TestSubject"
