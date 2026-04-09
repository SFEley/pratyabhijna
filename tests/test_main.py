"""Tests for the ``__main__`` entry point module.

TDD: these tests define the server startup and CLI dispatch contracts.
The entry point starts the MCP server with a lifespan that manages
service/queue lifecycle, or dispatches to the seed subcommand.
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module importability
# ---------------------------------------------------------------------------

class TestMainImport:
    def test_main_module_importable(self):
        """The __main__ module can be imported without side effects."""
        import pratyabhijna.__main__  # noqa: F401


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

class TestLifespan:
    async def test_lifespan_starts_and_stops_service(self):
        """Entering lifespan starts service and queue; exiting stops them."""
        from pratyabhijna.__main__ import build_lifespan
        from pratyabhijna.tools.remember import make_handler as remember_handler
        from pratyabhijna.tools.correct import make_handler as correct_handler

        service = MagicMock()
        service.start = AsyncMock()
        service.stop = AsyncMock()

        queue = MagicMock()
        queue.start = AsyncMock()
        queue.stop = AsyncMock()
        queue.register = MagicMock()

        lifespan_cm = build_lifespan(service, queue)
        app = MagicMock()

        async with lifespan_cm(app):
            service.start.assert_called_once()
            queue.start.assert_called_once()

        queue.stop.assert_called_once()
        service.stop.assert_called_once()

    async def test_lifespan_registers_queue_handlers(self):
        """Lifespan registers add_episode and correct_memory handlers."""
        from pratyabhijna.__main__ import build_lifespan

        service = MagicMock()
        service.start = AsyncMock()
        service.stop = AsyncMock()

        queue = MagicMock()
        queue.start = AsyncMock()
        queue.stop = AsyncMock()
        queue.register = MagicMock()

        lifespan_cm = build_lifespan(service, queue)
        app = MagicMock()

        async with lifespan_cm(app):
            pass

        registered_types = [call.args[0] for call in queue.register.call_args_list]
        assert "add_episode" in registered_types
        assert "correct_memory" in registered_types

    async def test_lifespan_stops_on_exception(self):
        """Service and queue are stopped even if an error occurs."""
        from pratyabhijna.__main__ import build_lifespan

        service = MagicMock()
        service.start = AsyncMock()
        service.stop = AsyncMock()

        queue = MagicMock()
        queue.start = AsyncMock()
        queue.stop = AsyncMock()
        queue.register = MagicMock()

        lifespan_cm = build_lifespan(service, queue)
        app = MagicMock()

        with pytest.raises(RuntimeError, match="test error"):
            async with lifespan_cm(app):
                raise RuntimeError("test error")

        queue.stop.assert_called_once()
        service.stop.assert_called_once()


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

class TestCLIDispatch:
    def test_seed_subcommand_dispatches(self):
        """sys.argv = ['pratyabhijna', 'seed'] routes to seed logic."""
        from pratyabhijna.__main__ import main

        with patch.object(sys, "argv", ["pratyabhijna", "seed"]), \
             patch("pratyabhijna.__main__.run_seed") as mock_seed, \
             patch("pratyabhijna.__main__.configure_logging"), \
             patch("pratyabhijna.__main__.PratyabhijnaConfig") as mock_config_cls:
            mock_config_cls.from_env.return_value = MagicMock()
            main()
            mock_seed.assert_called_once()

    def test_no_args_runs_server(self):
        """No subcommand starts the MCP server."""
        from pratyabhijna.__main__ import main

        with patch.object(sys, "argv", ["pratyabhijna"]), \
             patch("pratyabhijna.__main__.configure_logging"), \
             patch("pratyabhijna.__main__.PratyabhijnaConfig") as mock_config_cls, \
             patch("pratyabhijna.__main__.PratyabhijnaService"), \
             patch("pratyabhijna.__main__.WorkQueue"), \
             patch("pratyabhijna.__main__.create_server") as mock_create:
            mock_config_cls.from_env.return_value = MagicMock()
            mock_server = MagicMock()
            mock_create.return_value = mock_server
            main()
            mock_server.run.assert_called_once_with(transport="stdio")
