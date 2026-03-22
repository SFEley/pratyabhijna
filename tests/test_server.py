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
]


class TestServerToolRegistration:
    """Server registers all expected MCP tools."""

    def test_server_creates(self):
        """Server object can be instantiated."""
        from pratyabhijna.server import create_server

        server = create_server()
        assert server is not None

    def test_server_has_name(self):
        """Server identifies itself as Pratyabhijna."""
        from pratyabhijna.server import create_server

        server = create_server()
        assert server.name == "pratyabhijna"

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
