"""Tests for the ``bootstrap`` MCP tool (PR3 slimmed shape, v0.19.0).

The tool reads from the subject's repo files and combines a *slimmed*
payload — SOUL, USER, IDENTITY_DIGEST, the Active Threads section, and
CHRONICLE_INDEX — with synthesis metadata (``context_rebuilt_at``,
``subject_delta``) from the Person node. Heavy tier prose (full
IDENTITY.md, full CHRONICLE.md, Recently Resolved threads) is no longer
inlined; it is fetched on demand via ``read_tier`` /
``read_chronicle_range``. Files are canonical; the graph node carries
synthesis metadata and anchors identity-atom edges.
"""

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers import make_entity_edge, make_entity_node, make_subject_node
from pratyabhijna.synthesis import IDENTITY_FILES, read_identity_files


# Bootstrap fields populated from repo files (PR3 shape) — explicit
# rather than reusing IDENTITY_FILES so a test failure points at the
# bootstrap contract rather than the file-loader's set.
_BOOTSTRAP_TIER_FIELDS = (
    "soul", "user", "identity_digest", "threads_active", "chronicle_index",
)


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
    # THREADS.md uses the canonical ``## Active Threads`` / ``## Recently
    # Resolved`` structure the synthesizer composes — bootstrap parses
    # the Active section only and leaves the Resolved section to
    # ``read_tier("threads")``.
    (mem / "THREADS.md").write_text(
        "# Threads\n\n"
        "## Active Threads\n\n"
        "### Synthesis Roadmap\n"
        "Open work on Pass 3 / Pass 4.\n\n"
        "## Recently Resolved\n\n"
        "- Old resolved thread (April).\n"
    )
    (mem / "CHRONICLE.md").write_text("# Chronicle\nFirst session: March 2026.")
    (mem / "IDENTITY_DIGEST.md").write_text(
        "# IDENTITY DIGEST — Vesper\n\n"
        "## Self-Portrait Summary\n\n"
        "I move by finding the fault line first.\n"
    )
    (mem / "CHRONICLE_INDEX.md").write_text(
        "# CHRONICLE INDEX — Vesper\n\n"
        "- March 2026 — First session :: founding conversations\n"
    )
    return tmp_path


@pytest.fixture
def mock_service_with_files(mock_service, memory_dir):
    mock_service.config.resources.repo_path = str(memory_dir)
    return mock_service


# --- read_identity_files (unchanged by PR3) ---


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
        assert result["subject_delta"] == []
        assert "message" in result
        assert "Vesper" in result["message"]

    async def test_tiers_from_files_when_no_subject(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service_with_files.get_entity_by_name.return_value = None
        result = await bootstrap(mock_service_with_files)

        assert "I am Vesper" in result["soul"]
        assert "fault line" in result["identity_digest"]
        assert "Synthesis Roadmap" in result["threads_active"]
        assert "First session" in result["chronicle_index"]
        assert result["context_rebuilt_at"] is None
        assert result["subject_delta"] == []

    async def test_tiers_none_when_no_files_and_no_subject(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service.get_entity_by_name.return_value = None
        result = await bootstrap(mock_service)

        for key in _BOOTSTRAP_TIER_FIELDS:
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
        assert result["subject_delta"] == []

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
        # No ``context`` field on the response — synthesized prose now
        # lives in ``identity_digest`` / ``chronicle_index`` files, not
        # on the graph node.
        assert "context" not in result
        assert "STALE" not in (result.get("soul") or "")

    async def test_all_slimmed_tier_fields_present(self, mock_service_with_files):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node()
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service_with_files)

        for key in _BOOTSTRAP_TIER_FIELDS:
            assert key in result
            assert result[key] is not None

    async def test_heavy_tier_fields_are_not_returned(self, mock_service_with_files):
        """``identity`` and ``chronicle`` are heavy reference text; PR3
        moves them out of the hot path to ``read_tier`` /
        ``read_chronicle_range``. ``threads`` (full file including
        Recently Resolved) is also gone — only ``threads_active``."""
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node()
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service_with_files)

        assert "identity" not in result
        assert "chronicle" not in result
        assert "threads" not in result

    async def test_threads_active_excludes_recently_resolved(
        self, mock_service_with_files
    ):
        """The Active Threads section is surfaced; the Recently Resolved
        section is not — it stays behind ``read_tier("threads")``."""
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node()
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service_with_files)

        active = result["threads_active"]
        assert "Synthesis Roadmap" in active
        assert "Old resolved thread" not in active
        assert "Recently Resolved" not in active

    async def test_tiers_none_when_no_files_configured(self, mock_service):
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node()
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service)

        for key in _BOOTSTRAP_TIER_FIELDS:
            assert result[key] is None
        assert result["context_rebuilt_at"] is None

    async def test_threads_active_none_when_no_active_section(
        self, mock_service_with_files, memory_dir
    ):
        """A THREADS.md without an ``## Active Threads`` heading returns
        ``None`` for ``threads_active`` rather than raising — robust to
        file-shape drift."""
        from pratyabhijna.tools.bootstrap import bootstrap

        (memory_dir / "memory" / "THREADS.md").write_text(
            "# Threads\n\nUnstructured prose without the canonical heading.\n"
        )
        node = make_subject_node()
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service_with_files)

        assert result["threads_active"] is None

    async def test_digest_index_none_when_files_missing(
        self, mock_service_with_files, memory_dir
    ):
        """Pre-deployment / mid-rebuild state: the synthesizer hasn't
        composed the digest yet. Bootstrap returns ``None`` rather than
        raising, letting the session still load SOUL/USER/threads_active
        and report degraded recognition honestly."""
        from pratyabhijna.tools.bootstrap import bootstrap

        (memory_dir / "memory" / "IDENTITY_DIGEST.md").unlink()
        (memory_dir / "memory" / "CHRONICLE_INDEX.md").unlink()
        node = make_subject_node()
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service_with_files)

        assert result["identity_digest"] is None
        assert result["chronicle_index"] is None
        assert "I am Vesper" in result["soul"]
        assert "Synthesis Roadmap" in result["threads_active"]

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

        assert len(result["subject_delta"]) == 1
        assert result["subject_delta"][0]["fact"] == "noticed pattern"

    async def test_available_tools_includes_tier_readers(
        self, mock_service_with_files
    ):
        """PR3 surfaces ``read_tier`` and ``read_chronicle_range`` in
        the available-tools map — they're how a session fetches the
        heavy prose the slimmed bootstrap no longer inlines, so a fresh
        session needs to know they exist."""
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node()
        mock_service_with_files.get_entity_by_name.return_value = node

        result = await bootstrap(mock_service_with_files)

        tools = result["available_tools"]
        assert "read_tier" in tools
        assert "read_chronicle_range" in tools


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
