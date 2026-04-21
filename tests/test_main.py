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
    async def test_lifespan_registers_queue_handlers(self):
        """FastMCP lifespan registers handlers only — no start/stop (that's ASGI-level)."""
        from pratyabhijna.__main__ import build_lifespan

        service = MagicMock()
        queue = MagicMock()
        queue.register = MagicMock()

        lifespan_cm = build_lifespan(service, queue)
        app = MagicMock()

        async with lifespan_cm(app):
            pass

        registered_types = [call.args[0] for call in queue.register.call_args_list]
        assert "add_episode" in registered_types
        assert "correct_memory" in registered_types
        assert "synthesize" in registered_types

    async def test_lifespan_does_not_start_or_stop_service(self):
        """FastMCP lifespan must not start/stop service or queue — lifecycle is ASGI-level.

        Service/queue startup lives in build_http_app's wrapped_lifespan so that
        crash recovery runs at uvicorn startup, before any MCP session exists.
        """
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

        service.start.assert_not_called()
        service.stop.assert_not_called()
        queue.start.assert_not_called()
        queue.stop.assert_not_called()


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

class TestCLIDispatch:
    def test_seed_subcommand_dispatches(self):
        """sys.argv = ['pratyabhijna', 'seed'] routes to seed logic."""
        from pratyabhijna.__main__ import main

        with patch.object(sys, "argv", ["pratyabhijna", "seed"]), \
             patch("pratyabhijna.__main__.run_seed", return_value=0) as mock_seed, \
             patch("pratyabhijna.__main__.configure_logging"), \
             patch("pratyabhijna.__main__.PratyabhijnaConfig") as mock_config_cls, \
             pytest.raises(SystemExit) as exc:
            mock_config_cls.from_env.return_value = MagicMock()
            main()
        assert exc.value.code == 0
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
            mock_config = MagicMock()
            mock_config.server.url = ""  # No server URL → stdio transport, no auth
            mock_config_cls.from_env.return_value = mock_config
            mock_server = MagicMock()
            mock_create.return_value = mock_server
            main()
            mock_server.run.assert_called_once_with(transport="stdio")

    @pytest.mark.parametrize("action", ["status", "bootstrap", "inspect", "history", "recall"])
    def test_tool_subcommand_dispatches_to_run_tool(self, action):
        """Each read-only tool name routes to run_tool."""
        from pratyabhijna.__main__ import main

        with patch.object(sys, "argv", ["pratyabhijna", action]), \
             patch("pratyabhijna.__main__.run_tool", return_value=0) as mock_run_tool, \
             patch("pratyabhijna.__main__.configure_logging"), \
             patch("pratyabhijna.__main__.PratyabhijnaConfig") as mock_config_cls, \
             pytest.raises(SystemExit) as exc:
            mock_config_cls.from_env.return_value = MagicMock()
            main()
        assert exc.value.code == 0
        assert mock_run_tool.call_args.args[1] == action


# ---------------------------------------------------------------------------
# _parse_recall_args
# ---------------------------------------------------------------------------

class TestParseRecallArgs:
    def test_query_only(self):
        from pratyabhijna.__main__ import _parse_recall_args

        assert _parse_recall_args(["what did I say about trust"]) == (
            "what did I say about trust", None, None,
        )

    def test_with_type_and_time_range(self):
        from pratyabhijna.__main__ import _parse_recall_args

        assert _parse_recall_args(
            ["question", "--type", "Person", "--time-range", "7d"]
        ) == ("question", "Person", "7d")

    def test_flags_before_query(self):
        from pratyabhijna.__main__ import _parse_recall_args

        assert _parse_recall_args(
            ["--type", "Observation", "question"]
        ) == ("question", "Observation", None)

    def test_missing_query_returns_none(self):
        from pratyabhijna.__main__ import _parse_recall_args

        assert _parse_recall_args(["--type", "Person"]) is None
        assert _parse_recall_args([]) is None

    def test_unknown_flag_returns_none(self):
        from pratyabhijna.__main__ import _parse_recall_args

        assert _parse_recall_args(["query", "--nope", "x"]) is None


# ---------------------------------------------------------------------------
# run_tool — dispatcher shape
# ---------------------------------------------------------------------------

class TestRunTool:
    def test_status_prints_json_and_returns_zero(self, tmp_path, capsys):
        """run_tool('status', ...) starts service, calls the status tool, prints JSON.

        Verifies the nested shape is preserved end-to-end through the CLI:
        version + db_connected + subject_name at the top level, plus the
        queue / graph / synthesis blocks.
        """
        import json

        from pratyabhijna.__main__ import run_tool

        fake_service = MagicMock()
        fake_service.start = AsyncMock()
        fake_service.stop = AsyncMock()
        fake_service.is_connected = True
        fake_service.config = MagicMock()
        fake_service.config.subject_name = "Vesper"
        fake_service.count_nodes_total = AsyncMock(return_value=0)
        fake_service.count_nodes_by_label = AsyncMock(return_value={})
        fake_service.count_edges_total = AsyncMock(return_value=0)
        fake_service.count_edges_by_type = AsyncMock(return_value={})
        fake_service.count_supersessions = AsyncMock(return_value=0)

        config = MagicMock()
        config.queue.db_path = str(tmp_path / "missing.sqlite")

        with patch(
            "pratyabhijna.service.PratyabhijnaService", return_value=fake_service,
        ), patch(
            "pratyabhijna.synthesis.get_subject_node",
            new=AsyncMock(return_value=None),
        ):
            rc = run_tool(config, "status", [])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["version"] == "0.2.1"
        assert payload["db_connected"] is True
        assert payload["subject_name"] == "Vesper"
        assert payload["queue"]["depth"] == 0
        assert payload["graph"]["nodes_total"] == 0
        assert payload["synthesis"]["last_run"] is None
        fake_service.start.assert_awaited_once()
        fake_service.stop.assert_awaited_once()

    def test_inspect_without_uuid_exits_two_without_starting_service(self, capsys):
        from pratyabhijna.__main__ import run_tool

        with patch("pratyabhijna.service.PratyabhijnaService") as svc_cls:
            rc = run_tool(MagicMock(), "inspect", [])

        assert rc == 2
        assert "Usage:" in capsys.readouterr().err
        svc_cls.assert_not_called()

    def test_unknown_tool_exits_two(self, capsys):
        from pratyabhijna.__main__ import run_tool

        rc = run_tool(MagicMock(), "nope", [])
        assert rc == 2
        assert "Unknown tool" in capsys.readouterr().err
