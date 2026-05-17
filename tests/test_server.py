"""Tests for Pratyabhijna MCP server startup and tool registration.

Verifies that the server starts and exposes all expected tools,
per Phase 1 requirements.
"""

import pytest


EXPECTED_TOOLS = [
    "remember",
    "correct",
    "recall",
    "bootstrap",
    "history",
    "inspect",
    "status",
    "communities",
    "query",
    "read_tier",
    "read_chronicle_range",
]


class TestServerToolRegistration:
    """Server registers all expected MCP tools."""

    def test_server_creates(self):
        """Server object can be instantiated."""
        from pratyabhijna.server import create_server

        server = create_server()
        assert server is not None

    def test_server_has_name(self):
        """Server identifies itself as 'Pratyabhijna' regardless of subject."""
        from pratyabhijna.server import create_server

        server = create_server()
        assert server.name == "Pratyabhijna"

    def test_server_name_independent_of_subject(self):
        """Passing a subject_name does not change the advertised server name."""
        from pratyabhijna.server import create_server

        server = create_server(subject_name="Aria")
        assert server.name == "Pratyabhijna"

    @pytest.mark.parametrize("tool_name", EXPECTED_TOOLS)
    def test_tool_registered(self, tool_name):
        """Each expected tool is registered with the server."""
        from pratyabhijna.server import create_server

        server = create_server()
        # FastMCP stores tools in a dict keyed by name
        tool_names = list(server._tool_manager._tools.keys())
        assert tool_name in tool_names, (
            f"Tool '{tool_name}' not registered. "
            f"Registered tools: {tool_names}"
        )

    def test_no_unexpected_tools(self):
        """Only the expected tools are registered (no extras)."""
        from pratyabhijna.server import create_server

        server = create_server()
        tool_names = set(server._tool_manager._tools.keys())
        expected = set(EXPECTED_TOOLS)
        unexpected = tool_names - expected
        assert not unexpected, f"Unexpected tools registered: {unexpected}"

    def test_no_resources_without_repo_path(self):
        """No resources registered when repo_path is not provided."""
        from pratyabhijna.server import create_server

        server = create_server()
        assert len(server._resource_manager._resources) == 0
        assert len(server._resource_manager._templates) == 0

    def test_resources_registered_with_repo_path(self, tmp_path):
        """Resources registered when a valid repo_path is provided."""
        (tmp_path / "memory").mkdir()
        (tmp_path / "writing").mkdir()
        from pratyabhijna.server import create_server

        server = create_server(
            repo_path=str(tmp_path),
            resource_directories=["memory", "writing"],
        )
        assert len(server._resource_manager._resources) == 1
        assert len(server._resource_manager._templates) == 2
