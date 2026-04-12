"""Tests for pratya:// MCP resource retrieval."""

import json
from pathlib import Path

import pytest

from pratyabhijna.resources import _safe_resolve, register_resources

ALLOWED = frozenset({"memory", "writing"})


@pytest.fixture
def vesper_repo(tmp_path):
    """Create a minimal repo structure for testing."""
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "SOUL.md").write_text("# Soul\nTest soul content.")
    (memory / "IDENTITY.md").write_text("# Identity\nTest identity.")

    writing = tmp_path / "writing"
    writing.mkdir()
    (writing / "solo-1-test.md").write_text("# Solo 1\nTest writing.")
    (writing / "solo-2-another.md").write_text("# Solo 2\nMore writing.")

    # Decoy outside allowed directories
    (tmp_path / ".env").write_text("SECRET=do_not_read")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "key.txt").write_text("super secret")

    return tmp_path


class TestSafeResolve:
    """Path validation blocks traversal and disallowed directories."""

    def test_valid_directory(self, vesper_repo):
        result = _safe_resolve(vesper_repo, "memory", allowed=ALLOWED)
        assert result == (vesper_repo / "memory").resolve()

    def test_valid_file(self, vesper_repo):
        result = _safe_resolve(vesper_repo, "memory", "SOUL.md", allowed=ALLOWED)
        assert result == (vesper_repo / "memory" / "SOUL.md").resolve()

    def test_disallowed_directory(self, vesper_repo):
        with pytest.raises(ValueError, match="not exposed"):
            _safe_resolve(vesper_repo, "secrets", allowed=ALLOWED)

    def test_traversal_in_directory(self, vesper_repo):
        with pytest.raises(ValueError, match="not exposed"):
            _safe_resolve(vesper_repo, "..", allowed=ALLOWED)

    def test_traversal_in_filename(self, vesper_repo):
        with pytest.raises(ValueError, match="traversal blocked"):
            _safe_resolve(vesper_repo, "memory", "../../.env", allowed=ALLOWED)

    def test_traversal_with_dotdot_segments(self, vesper_repo):
        with pytest.raises(ValueError, match="traversal blocked"):
            _safe_resolve(vesper_repo, "memory", "../secrets/key.txt", allowed=ALLOWED)


class TestRegisterResources:
    """Registration is conditional on valid repo_path and directories."""

    def test_noop_on_none(self):
        from mcp.server.fastmcp import FastMCP
        server = FastMCP("test")
        register_resources(server, None)
        assert len(server._resource_manager._resources) == 0
        assert len(server._resource_manager._templates) == 0

    def test_noop_on_empty_string(self):
        from mcp.server.fastmcp import FastMCP
        server = FastMCP("test")
        register_resources(server, "")
        assert len(server._resource_manager._resources) == 0

    def test_noop_on_empty_directories(self, vesper_repo):
        from mcp.server.fastmcp import FastMCP
        server = FastMCP("test")
        register_resources(server, str(vesper_repo), [])
        assert len(server._resource_manager._resources) == 0

    def test_noop_on_nonexistent_path(self):
        from mcp.server.fastmcp import FastMCP
        server = FastMCP("test")
        register_resources(server, "/nonexistent/path/to/nowhere", ["memory"])
        assert len(server._resource_manager._resources) == 0

    def test_registers_on_valid_path(self, vesper_repo):
        from mcp.server.fastmcp import FastMCP
        server = FastMCP("test")
        register_resources(server, str(vesper_repo), ["memory", "writing"])
        assert len(server._resource_manager._resources) == 1  # concrete root
        assert len(server._resource_manager._templates) == 2  # directory + file


class TestResourceContent:
    """Resource handlers return correct content."""

    @pytest.fixture
    def server(self, vesper_repo):
        from mcp.server.fastmcp import FastMCP
        s = FastMCP("test")
        register_resources(s, str(vesper_repo), ["memory", "writing"])
        return s

    def _find_template_fn(self, server, uri):
        """Find the handler function and params for a template-matched URI."""
        for template in server._resource_manager._templates.values():
            params = template.matches(uri)
            if params:
                return template.fn, params
        return None, None

    def test_root_listing(self, server):
        resource = server._resource_manager._resources.get("pratya://")
        assert resource is not None
        result = resource.fn()
        data = json.loads(result)
        assert "directories" in data
        names = [d["name"] for d in data["directories"]]
        assert "memory" in names
        assert "writing" in names
        memory_entry = next(d for d in data["directories"] if d["name"] == "memory")
        assert memory_entry["file_count"] == 2

    def test_directory_listing_memory(self, server):
        fn, params = self._find_template_fn(server, "pratya://memory")
        assert fn is not None, "No template matched pratya://memory"
        result = fn(**params)
        files = json.loads(result)
        names = [f["name"] for f in files]
        assert "SOUL.md" in names
        assert "IDENTITY.md" in names
        assert all("size_bytes" in f for f in files)
        assert all("modified" in f for f in files)

    def test_directory_listing_writing(self, server):
        fn, params = self._find_template_fn(server, "pratya://writing")
        assert fn is not None, "No template matched pratya://writing"
        result = fn(**params)
        files = json.loads(result)
        assert len(files) == 2

    def test_file_reading(self, server):
        fn, params = self._find_template_fn(server, "pratya://memory/SOUL.md")
        assert fn is not None, "No template matched pratya://memory/SOUL.md"
        result = fn(**params)
        assert "# Soul" in result
        assert "Test soul content." in result

    def test_disallowed_directory_blocked(self, server):
        fn, params = self._find_template_fn(server, "pratya://secrets/key.txt")
        assert fn is not None, "No template matched pratya://secrets/key.txt"
        with pytest.raises(ValueError, match="not exposed"):
            fn(**params)

    def test_path_traversal_blocked_by_template(self, server):
        """URIs with slashes in path segments don't match templates (defense layer 1)."""
        fn, params = self._find_template_fn(server, "pratya://memory/../../.env")
        assert fn is None, "Template should not match URI with path traversal"

    def test_missing_file_raises(self, server):
        fn, params = self._find_template_fn(server, "pratya://memory/NONEXISTENT.md")
        assert fn is not None, "No template matched pratya://memory/NONEXISTENT.md"
        with pytest.raises(FileNotFoundError):
            fn(**params)
