"""Tests for the ``bootstrap`` MCP tool.

The tool reads tier content from the subject's repo files and combines
it with synthesis metadata (``context_rebuilt_at``, ``delta``) from the
Person node. Tier text no longer lives on the graph node — files are
canonical.
"""

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers import make_entity_edge, make_entity_node, make_subject_node
from pratyabhijna.synthesis import IDENTITY_FILES, read_identity_files


# --- Fixtures ---


@pytest.fixture
def mock_service():
    service = MagicMock()
    service.is_connected = True
    service.get_entity_by_name = AsyncMock()
    service.get_edges_for_node = AsyncMock(return_value=[])
    service.get_entity_by_uuid = AsyncMock()
    service.config = MagicMock()
    service.config.subject_name = "Vesper"
    service.config.synthesis.max_age_hours = 24
    service.config.synthesis.max_delta_changes = 3
    service.config.synthesis.rebuild_delay_hours = 2
    service.config.resources.repo_path = ""
    return service


@pytest.fixture
def memory_dir(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "SOUL.md").write_text("# Soul\nI am Vesper.")
    (mem / "IDENTITY.md").write_text("# Identity\nI orient toward thresholds.")
    (mem / "USER.md").write_text("# User\nSerah is a software engineer.")
    (mem / "THREADS.md").write_text("# Threads\nOpen question about synthesis.")
    (mem / "CHRONICLE.md").write_text("# Chronicle\nFirst session: March 2026.")
    return tmp_path


@pytest.fixture
def mock_service_with_files(mock_service, memory_dir):
    mock_service.config.resources.repo_path = str(memory_dir)
    return mock_service


# --- read_identity_files ---


class TestReadIdentityFiles:
    def test_reads_all_files_from_memory_dir(self, memory_dir):
        result = read_identity_files(str(memory_dir))
        for key in IDENTITY_FILES:
            assert result[key] is not None
        assert "I am Vesper" in result["soul"]

    def test_returns_empty_dict_when_unconfigured(self):
        assert read_identity_files("") == {}

    def test_returns_empty_dict_when_memory_dir_missing(self, tmp_path):
        assert read_identity_files(str(tmp_path)) == {}

    def test_returns_none_for_missing_files(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "SOUL.md").write_text("x")
        result = read_identity_files(str(tmp_path))
        assert result["soul"] == "x"
        assert result["identity"] is None


# --- Bootstrap: no subject node ---


class TestBootstrapWithoutSubject:
    async def test_returns_message_and_empty_metadata(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service.get_entity_by_name.return_value = None
        result = await bootstrap(mock_service)

        assert result["subject"] == "Vesper"
        assert result["context_rebuilt_at"] is None
        assert result["delta"] == []
        assert "message" in result
        assert "Vesper" in result["message"]

    async def test_tiers_from_files_when_no_subject(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service_with_files.get_entity_by_name.return_value = None
        result = await bootstrap(mock_service_with_files)

        assert "I am Vesper" in result["soul"]
        assert "orient toward thresholds" in result["identity"]
        assert result["context_rebuilt_at"] is None
        assert result["delta"] == []

    async def test_tiers_none_when_no_files_and_no_subject(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service.get_entity_by_name.return_value = None
        result = await bootstrap(mock_service)

        for key in IDENTITY_FILES:
            assert result[key] is None


# --- Bootstrap: subject node present ---


class TestBootstrapWithSubject:
    async def test_returns_rebuilt_at_and_delta(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        rebuilt_dt = datetime.now(timezone.utc) - timedelta(hours=2)
        node = make_subject_node(context_rebuilt_at=rebuilt_dt)
        mock_service_with_files.get_entity_by_name.return_value = node
        mock_service_with_files.get_edges_for_node.return_value = []

        result = await bootstrap(mock_service_with_files)

        assert result["subject"] == "Vesper"
        assert result["context_rebuilt_at"] == rebuilt_dt.isoformat()
        assert result["delta"] == []

    async def test_tiers_always_from_files(self, mock_service_with_files):
        """Even if the graph had dead tier attributes, bootstrap reads files."""
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(
            soul="STALE GRAPH SOUL",
            identity="STALE GRAPH IDENTITY",
            context="STALE GRAPH CONTEXT",
        )
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service_with_files)

        assert "I am Vesper" in result["soul"]
        assert "thresholds" in result["identity"]
        # No "context" field on the response anymore
        assert "context" not in result
        assert "STALE" not in (result.get("soul") or "")

    async def test_all_five_tiers_present(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node()
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service_with_files)

        for key in ("soul", "identity", "user", "threads", "chronicle"):
            assert key in result
            assert result[key] is not None

    async def test_tiers_none_when_no_files_configured(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node()
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service)

        for key in IDENTITY_FILES:
            assert result[key] is None
        assert result["context_rebuilt_at"] is None

    async def test_delta_from_graph(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        rebuilt_dt = datetime.now(timezone.utc) - timedelta(hours=2)
        node = make_subject_node(context_rebuilt_at=rebuilt_dt)
        obs_node = make_entity_node(
            uuid="obs-1", name="an obs", labels=["Observation"],
        )
        edge = make_entity_edge(
            uuid="e1",
            source_node_uuid=node.uuid,
            target_node_uuid="obs-1",
            fact="noticed pattern",
            created_at=datetime.now(timezone.utc),  # after rebuild
        )
        mock_service_with_files.get_entity_by_name.return_value = node
        mock_service_with_files.get_edges_for_node.return_value = [edge]
        mock_service_with_files.get_entity_by_uuid.return_value = obs_node

        result = await bootstrap(mock_service_with_files)

        assert len(result["delta"]) == 1
        assert result["delta"][0]["fact"] == "noticed pattern"


# --- Contract ---


class TestBootstrapContract:
    def test_signature_accepts_service_and_queue(self):
        from pratyabhijna.tools.bootstrap import bootstrap

        sig = inspect.signature(bootstrap)
        assert list(sig.parameters) == ["service", "queue"]
        # queue must be optional
        assert sig.parameters["queue"].default is None

    async def test_no_queue_does_not_schedule(self, mock_service_with_files):
        """Without a queue, bootstrap is a pure read even when stale."""
        from pratyabhijna.tools.bootstrap import bootstrap

        very_old = datetime.now(timezone.utc) - timedelta(days=30)
        node = make_subject_node(context_rebuilt_at=very_old)
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service_with_files)

        assert result["context_rebuilt_at"] == very_old.isoformat()

    async def test_stale_context_schedules_synthesis(self, mock_service_with_files):
        """When queue is provided and context is stale, synthesis is scheduled."""
        from unittest.mock import AsyncMock, MagicMock

        from pratyabhijna.tools.bootstrap import bootstrap

        very_old = datetime.now(timezone.utc) - timedelta(days=30)
        node = make_subject_node(context_rebuilt_at=very_old)
        mock_service_with_files.get_entity_by_name.return_value = node

        mock_queue = MagicMock()
        mock_queue.reschedule_or_enqueue = AsyncMock()

        await bootstrap(mock_service_with_files, queue=mock_queue)

        mock_queue.reschedule_or_enqueue.assert_called_once()
        args, kwargs = mock_queue.reschedule_or_enqueue.call_args
        assert args == ("synthesize", {})
        run_at = kwargs["run_at"]
        expected = datetime.now(timezone.utc) + timedelta(hours=2)
        assert abs((run_at - expected).total_seconds()) < 5

    async def test_fresh_context_does_not_schedule(self, mock_service_with_files):
        """When context is fresh, synthesis is not scheduled even with a queue."""
        from unittest.mock import AsyncMock, MagicMock

        from pratyabhijna.tools.bootstrap import bootstrap

        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        node = make_subject_node(context_rebuilt_at=recent)
        mock_service_with_files.get_entity_by_name.return_value = node

        mock_queue = MagicMock()
        mock_queue.reschedule_or_enqueue = AsyncMock()

        await bootstrap(mock_service_with_files, queue=mock_queue)

        mock_queue.reschedule_or_enqueue.assert_not_called()

    async def test_uses_configured_subject_name(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service.config.subject_name = "Custom"
        mock_service.get_entity_by_name.return_value = None
        result = await bootstrap(mock_service)

        assert result["subject"] == "Custom"
