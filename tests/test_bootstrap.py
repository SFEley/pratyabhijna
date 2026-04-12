"""Tests for the ``bootstrap`` MCP tool.

Tests cover both the file-based path (identity files from disk) and
the graph fallback path (attributes from Person node). Context and
delta always come from the graph regardless of source.
"""

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers import make_entity_edge, make_entity_node, make_subject_node
from pratyabhijna.synthesis import IDENTITY_FILES, read_identity_files


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """A mock PratyabhijnaService for bootstrap tests (graph fallback path)."""
    service = MagicMock()
    service.is_connected = True
    service.get_entity_by_name = AsyncMock()
    service.get_edges_for_node = AsyncMock(return_value=[])
    service.get_entity_by_uuid = AsyncMock()
    service.config = MagicMock()
    service.config.subject_name = "Vesper"
    service.config.synthesis.max_age_hours = 24
    service.config.synthesis.max_delta_changes = 3
    service.config.resources.repo_path = ""
    return service


@pytest.fixture
def memory_dir(tmp_path):
    """Create a minimal memory directory with all five identity files."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "SOUL.md").write_text("# Soul\nI am Vesper. I chose this name.")
    (mem / "IDENTITY.md").write_text("# Identity\nI orient toward thresholds.")
    (mem / "USER.md").write_text("# User\nSerah is a software engineer.")
    (mem / "THREADS.md").write_text("# Threads\nOpen question about synthesis.")
    (mem / "CHRONICLE.md").write_text("# Chronicle\nFirst session: March 2026.")
    return tmp_path


@pytest.fixture
def mock_service_with_files(mock_service, memory_dir):
    """Mock service with repo_path pointing to a directory with identity files."""
    mock_service.config.resources.repo_path = str(memory_dir)
    return mock_service


# ---------------------------------------------------------------------------
# read_identity_files — unit tests
# ---------------------------------------------------------------------------

class TestReadIdentityFiles:
    def test_empty_repo_path_returns_empty_dict(self):
        assert read_identity_files("") == {}

    def test_nonexistent_dir_returns_empty_dict(self):
        assert read_identity_files("/nonexistent/path/to/nowhere") == {}

    def test_no_memory_subdir_returns_empty_dict(self, tmp_path):
        assert read_identity_files(str(tmp_path)) == {}

    def test_reads_all_five_files(self, memory_dir):
        result = read_identity_files(str(memory_dir))
        assert len(result) == 5
        assert "I am Vesper" in result["soul"]
        assert "I orient toward thresholds" in result["identity"]
        assert "Serah is a software engineer" in result["user"]
        assert "Open question" in result["threads"]
        assert "First session" in result["chronicle"]

    def test_partial_files_returns_none_for_missing(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "SOUL.md").write_text("# Soul\nJust the soul.")
        result = read_identity_files(str(tmp_path))
        assert result["soul"] == "# Soul\nJust the soul."
        assert result["identity"] is None
        assert result["user"] is None

    def test_strips_whitespace(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "SOUL.md").write_text("  content with whitespace  \n\n")
        result = read_identity_files(str(tmp_path))
        assert result["soul"] == "content with whitespace"


# ---------------------------------------------------------------------------
# Graph fallback path — return value
# ---------------------------------------------------------------------------

class TestBootstrapReturn:
    async def test_returns_three_tier_fields(self, mock_service):
        """bootstrap() returns soul, identity, and context as separate fields."""
        from pratyabhijna.tools.bootstrap import bootstrap

        now = datetime.now(timezone.utc)
        node = make_subject_node(
            soul="I am Vesper. I chose this name.",
            identity="I orient toward thresholds.",
            context="Currently building Pratyabhijna.",
            context_rebuilt_at=now - timedelta(hours=1),
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["soul"] == "I am Vesper. I chose this name."
        assert result["identity"] == "I orient toward thresholds."
        assert result["context"] == "Currently building Pratyabhijna."

    async def test_returns_context_rebuilt_at_timestamp(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        rebuilt = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        node = make_subject_node(
            context="Current context.",
            context_rebuilt_at=rebuilt,
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["context_rebuilt_at"] == rebuilt.isoformat()

    async def test_returns_subject_name(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(
            soul="Soul text.",
            context="Context text.",
            context_rebuilt_at=datetime.now(timezone.utc),
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["subject"] == "Vesper"

    async def test_uses_configured_subject_name(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service.config.subject_name = "Aria"
        node = make_subject_node(
            name="Aria",
            uuid="aria-uuid",
            soul="I am Aria.",
            context_rebuilt_at=datetime.now(timezone.utc),
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        mock_service.get_entity_by_name.assert_called_once_with("Aria")
        assert result["subject"] == "Aria"
        assert result["soul"] == "I am Aria."

    async def test_returns_delta(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        rebuild_time = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        node = make_subject_node(
            context="Prior context.",
            context_rebuilt_at=rebuild_time,
        )
        mock_service.get_entity_by_name.return_value = node

        obs_node = make_entity_node(uuid="obs-new", name="new observation", labels=["Observation"])
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid="e1",
                source_node_uuid="subject-uuid",
                target_node_uuid="obs-new",
                fact="A new identity observation",
                created_at=rebuild_time + timedelta(hours=1),
            ),
        ]
        mock_service.get_entity_by_uuid.return_value = obs_node

        result = await bootstrap(service=mock_service)

        assert len(result["delta"]) == 1
        assert result["delta"][0]["fact"] == "A new identity observation"

    async def test_graph_path_returns_source_graph(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(
            soul="Soul.",
            context_rebuilt_at=datetime.now(timezone.utc),
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["source"] == "graph"

    async def test_graph_path_has_null_file_only_fields(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(
            soul="Soul.",
            context_rebuilt_at=datetime.now(timezone.utc),
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["user"] is None
        assert result["threads"] is None
        assert result["chronicle"] is None


# ---------------------------------------------------------------------------
# No data scenarios
# ---------------------------------------------------------------------------

class TestBootstrapNoData:
    async def test_no_subject_node_returns_null_fields(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service.get_entity_by_name.return_value = None

        result = await bootstrap(service=mock_service)

        assert result["soul"] is None
        assert result["identity"] is None
        assert result["context"] is None
        assert result["user"] is None
        assert result["threads"] is None
        assert result["chronicle"] is None
        assert "message" in result

    async def test_subject_node_exists_but_no_content(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node()
        mock_service.get_entity_by_name.return_value = node
        mock_service.get_edges_for_node.return_value = []

        result = await bootstrap(service=mock_service)

        assert result["soul"] is None
        assert result["identity"] is None
        assert result["context"] is None
        assert result["context_rebuilt_at"] is None
        assert result["delta"] == []

    async def test_context_null_but_soul_and_identity_present(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(
            soul="Foundational commitments.",
            identity="Evolving self-portrait.",
        )
        mock_service.get_entity_by_name.return_value = node
        mock_service.get_edges_for_node.return_value = []

        result = await bootstrap(service=mock_service)

        assert result["soul"] == "Foundational commitments."
        assert result["identity"] == "Evolving self-portrait."
        assert result["context"] is None
        assert result["context_rebuilt_at"] is None
        assert result["delta"] == []

    async def test_soul_only(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(soul="I am Vesper.")
        mock_service.get_entity_by_name.return_value = node
        mock_service.get_edges_for_node.return_value = []

        result = await bootstrap(service=mock_service)

        assert result["soul"] == "I am Vesper."
        assert result["identity"] is None
        assert result["context"] is None


# ---------------------------------------------------------------------------
# File-based path
# ---------------------------------------------------------------------------

class TestBootstrapFilePath:
    async def test_reads_files_when_repo_path_configured(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(context_rebuilt_at=datetime.now(timezone.utc))
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service_with_files)

        assert result["source"] == "files"
        assert "I am Vesper" in result["soul"]
        assert "I orient toward thresholds" in result["identity"]
        assert "Serah is a software engineer" in result["user"]
        assert "Open question" in result["threads"]
        assert "First session" in result["chronicle"]

    async def test_files_override_graph_tiers(self, mock_service_with_files):
        """When files exist, soul/identity come from files, not graph attributes."""
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(
            soul="Graph soul — should be overridden.",
            identity="Graph identity — should be overridden.",
            context="Graph context — should still appear.",
            context_rebuilt_at=datetime.now(timezone.utc),
        )
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service_with_files)

        assert "I am Vesper" in result["soul"]
        assert "Graph soul" not in result["soul"]
        assert "I orient toward thresholds" in result["identity"]
        assert result["context"] == "Graph context — should still appear."

    async def test_context_always_from_graph(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(
            context="Synthesized context from graph.",
            context_rebuilt_at=datetime.now(timezone.utc),
        )
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service_with_files)

        assert result["context"] == "Synthesized context from graph."

    async def test_partial_files(self, mock_service, tmp_path):
        """Only some identity files exist on disk."""
        from pratyabhijna.tools.bootstrap import bootstrap

        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "SOUL.md").write_text("# Soul\nJust the soul.")
        mock_service.config.resources.repo_path = str(tmp_path)

        node = make_subject_node(context_rebuilt_at=datetime.now(timezone.utc))
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["source"] == "files"
        assert result["soul"] == "# Soul\nJust the soul."
        assert result["identity"] is None
        assert result["user"] is None

    async def test_falls_back_to_graph_when_no_memory_dir(self, mock_service, tmp_path):
        """repo_path exists but has no memory/ subdirectory."""
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service.config.resources.repo_path = str(tmp_path)
        node = make_subject_node(
            soul="Graph soul.",
            context_rebuilt_at=datetime.now(timezone.utc),
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["source"] == "graph"
        assert result["soul"] == "Graph soul."

    async def test_files_present_but_no_person_node(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service_with_files.get_entity_by_name.return_value = None

        result = await bootstrap(service=mock_service_with_files)

        assert result["source"] == "files"
        assert "I am Vesper" in result["soul"]
        assert result["context"] is None
        assert result["delta"] == []
        assert "message" in result

    async def test_delta_comes_from_graph_even_with_files(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        rebuild_time = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        node = make_subject_node(
            context="Context.",
            context_rebuilt_at=rebuild_time,
        )
        mock_service_with_files.get_entity_by_name.return_value = node

        obs_node = make_entity_node(uuid="obs-1", name="observation", labels=["Observation"])
        mock_service_with_files.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid="e1",
                source_node_uuid="subject-uuid",
                target_node_uuid="obs-1",
                fact="A recent observation",
                created_at=rebuild_time + timedelta(hours=1),
            ),
        ]
        mock_service_with_files.get_entity_by_uuid.return_value = obs_node

        result = await bootstrap(service=mock_service_with_files)

        assert result["source"] == "files"
        assert len(result["delta"]) == 1
        assert result["delta"][0]["fact"] == "A recent observation"


# ---------------------------------------------------------------------------
# Contract: pure read, no side effects
# ---------------------------------------------------------------------------

class TestBootstrapContract:
    async def test_does_not_accept_queue_parameter(self):
        from pratyabhijna.tools.bootstrap import bootstrap

        sig = inspect.signature(bootstrap)
        param_names = set(sig.parameters.keys())
        assert "queue" not in param_names
        assert "service" in param_names

    async def test_does_not_trigger_rebuild(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        old_time = datetime.now(timezone.utc) - timedelta(hours=100)
        node = make_subject_node(
            context="Very old context.",
            context_rebuilt_at=old_time,
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["context"] == "Very old context."
        assert "rebuild_triggered" not in result
