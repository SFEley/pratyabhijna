"""Pratyabhijna MCP server entry point.

Usage:
    python -m pratyabhijna                       Start MCP server
    python -m pratyabhijna seed [--name NAME] [--soul-file PATH] [--identity-file PATH]
                                                 Seed subject identity

    python -m pratyabhijna status                System orientation
    python -m pratyabhijna bootstrap             Subject identity tiers
    python -m pratyabhijna inspect UUID          Node or edge detail
    python -m pratyabhijna history ENTITY        Entity relationship timeline
    python -m pratyabhijna recall QUERY [--type T] [--time-range R]

    python -m pratyabhijna deadletters list      Show dead-lettered tasks
    python -m pratyabhijna deadletters show ID   Show full detail for one
    python -m pratyabhijna deadletters retry ID  Reset to pending (or --all)
    python -m pratyabhijna deadletters purge ID  Delete permanently (or --all)
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.log import configure_logging, get_logger
from pratyabhijna.queue import WorkQueue
from pratyabhijna.server import create_server
from pratyabhijna.service import PratyabhijnaService
from pratyabhijna.tools.correct import make_handler as correct_make_handler
from pratyabhijna.tools.remember import make_handler as remember_make_handler

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = get_logger(__name__)


def build_lifespan(service: PratyabhijnaService, queue: WorkQueue):
    """Create the async lifespan context manager for the MCP server.

    Starts the service (Neo4j + Graphiti) and work queue on entry,
    registers queue handlers, and stops both on exit. OAuth storage
    lifecycle is handled separately in ``oauth.build_http_app`` — it
    needs to run at Starlette-app level, not inside this MCP-server
    lifespan which only fires when an MCP session is established.
    """

    @asynccontextmanager
    async def lifespan(app: FastMCP):
        await service.start()
        queue.register("add_episode", remember_make_handler(service))
        queue.register("correct_memory", correct_make_handler(service))
        await queue.start()
        log.info("Pratyabhijna server ready")
        try:
            yield
        finally:
            await queue.stop()
            await service.stop()
            log.info("Pratyabhijna server stopped")

    return lifespan


def _parse_seed_args(
    argv: list[str],
) -> tuple[str | None, str | None, str | None] | None:
    """Parse ``seed [--name NAME] [--soul-file PATH] [--identity-file PATH]``.

    Returns (name, soul_file, identity_file) or None on usage error.
    Any combination of flags may be omitted; callers fall back to config.
    """
    name: str | None = None
    soul_file: str | None = None
    identity_file: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--name" and i + 1 < len(argv):
            name = argv[i + 1]
            i += 2
        elif a == "--soul-file" and i + 1 < len(argv):
            soul_file = argv[i + 1]
            i += 2
        elif a == "--identity-file" and i + 1 < len(argv):
            identity_file = argv[i + 1]
            i += 2
        else:
            return None
    return name, soul_file, identity_file


def run_seed(config: PratyabhijnaConfig, argv: list[str]) -> int:
    """Run the seed subcommand synchronously.

    CLI flags override config values. ``subject_name`` and both file
    paths must be resolved (from CLI or config) before the service
    is started; otherwise returns exit code 2 with a usage error.
    """
    import anyio

    from pratyabhijna.seed import seed_subject

    parsed = _parse_seed_args(argv)
    if parsed is None:
        print(
            "Usage: python -m pratyabhijna seed "
            "[--name NAME] [--soul-file PATH] [--identity-file PATH]",
            file=sys.stderr,
        )
        return 2
    cli_name, cli_soul, cli_identity = parsed

    subject_name = cli_name or config.subject_name
    soul_path_str = cli_soul or config.seed.soul_path
    identity_path_str = cli_identity or config.seed.identity_path

    missing = []
    if not subject_name:
        missing.append("subject name (--name or config subject_name)")
    if not soul_path_str:
        missing.append("soul file (--soul-file or config seed.soul_path)")
    if not identity_path_str:
        missing.append("identity file (--identity-file or config seed.identity_path)")
    if missing:
        print("seed: missing required values:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        return 2

    from pathlib import Path

    soul_path = Path(soul_path_str).expanduser()
    identity_path = Path(identity_path_str).expanduser()

    # CLI --name overrides the config value in-place so the service
    # and seeder both see the same subject.
    if cli_name:
        config.subject_name = cli_name

    async def _run():
        service = PratyabhijnaService(config)
        await service.start()
        try:
            result = await seed_subject(
                service, soul_path=soul_path, identity_path=identity_path,
            )
            log.info("Seed result: %s", result)
        finally:
            await service.stop()

    anyio.run(_run)
    return 0


def run_deadletters(config: PratyabhijnaConfig, argv: list[str]) -> int:
    """Dispatch the ``deadletters`` subcommand group.

    Returns a process exit code (0 on success, 2 on usage error).
    """
    from pratyabhijna import deadletters as dl

    usage = (
        "Usage:\n"
        "  python -m pratyabhijna deadletters list\n"
        "  python -m pratyabhijna deadletters show ID\n"
        "  python -m pratyabhijna deadletters retry (ID | --all)\n"
        "  python -m pratyabhijna deadletters purge (ID | --all)"
    )

    if not argv:
        print(usage, file=sys.stderr)
        return 2

    db_path = config.queue.db_path
    from pathlib import Path
    if not Path(db_path).exists():
        print(f"Queue database not found at {db_path}", file=sys.stderr)
        return 2

    action, rest = argv[0], argv[1:]

    if action == "list":
        rows = dl.list_all(db_path)
        if not rows:
            print("No dead-lettered tasks.")
            return 0
        print(f"{len(rows)} dead-lettered task(s):\n")
        for r in rows:
            short = r.id[:8]
            err = r.error_summary
            if len(err) > 70:
                err = err[:67] + "..."
            print(
                f"  {short}  {r.task_type:<15} {r.attempts}/{r.max_attempts}  "
                f"{r.updated_at}  {err}"
            )
        return 0

    if action == "show":
        if len(rest) != 1:
            print("show requires a task id (or prefix)", file=sys.stderr)
            return 2
        try:
            d = dl.show(db_path, rest[0])
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"Task:     {d.id}")
        print(f"Type:     {d.task_type}")
        print(f"Status:   {d.status}")
        print(f"Attempts: {d.attempts}/{d.max_attempts}")
        print(f"Created:  {d.created_at}")
        print(f"Updated:  {d.updated_at}")
        print()
        print("Payload:")
        import json
        print(json.dumps(d.payload, indent=2))
        print()
        print("Error:")
        print(d.error)
        return 0

    if action in ("retry", "purge"):
        fn = dl.retry if action == "retry" else dl.purge
        verb_past = "Reset" if action == "retry" else "Deleted"

        if rest == ["--all"]:
            try:
                ids = fn(db_path, all_=True)
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
            if not ids:
                print("No dead-lettered tasks.")
            else:
                print(f"{verb_past} {len(ids)} task(s).")
            return 0

        if len(rest) == 1 and not rest[0].startswith("--"):
            try:
                ids = fn(db_path, task_id=rest[0])
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
            if not ids:
                print(f"No dead-lettered task matched '{rest[0]}'.")
                return 2
            print(f"{verb_past}: {ids[0]}")
            return 0

        print(f"{action} requires a task id or --all", file=sys.stderr)
        return 2

    print(f"Unknown deadletters action: {action}\n\n{usage}", file=sys.stderr)
    return 2


TOOL_COMMANDS = {"status", "bootstrap", "inspect", "history", "recall"}


def _cli_queue_stats(db_path: str) -> dict:
    """Read queue counters directly from SQLite.

    The ``status`` CLI subcommand needs queue depth, last write, and
    dead-letter info without starting a ``WorkQueue`` — starting one
    would run ``_recover_crashed`` (clobbering the live server's
    running task) and spawn a second worker loop. Direct SQL under
    WAL mode is safe and matches how ``deadletters`` reads the file.
    """
    import sqlite3
    from pathlib import Path

    if not Path(db_path).exists():
        return {
            "queue_depth": 0,
            "last_write": None,
            "dead_letters": 0,
            "last_error": None,
        }

    with sqlite3.connect(db_path, timeout=5.0) as conn:
        depth = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'running')"
        ).fetchone()[0]
        last_write_row = conn.execute(
            "SELECT completed_at FROM tasks WHERE status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        dead_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'dead_letter'"
        ).fetchone()[0]
        last_err_row = conn.execute(
            "SELECT id, error, updated_at FROM tasks WHERE status = 'dead_letter' "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()

    return {
        "queue_depth": depth,
        "last_write": last_write_row[0] if last_write_row else None,
        "dead_letters": dead_count,
        "last_error": (
            {"task_id": last_err_row[0], "error": last_err_row[1], "updated_at": last_err_row[2]}
            if last_err_row
            else None
        ),
    }


def _parse_recall_args(argv: list[str]) -> tuple[str, str | None, str | None] | None:
    """Parse ``recall QUERY [--type T] [--time-range R]``.

    Returns (query, memory_type, time_range) or None on usage error.
    """
    query: str | None = None
    memory_type: str | None = None
    time_range: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--type" and i + 1 < len(argv):
            memory_type = argv[i + 1]
            i += 2
        elif a == "--time-range" and i + 1 < len(argv):
            time_range = argv[i + 1]
            i += 2
        elif not a.startswith("--") and query is None:
            query = a
            i += 1
        else:
            return None
    if query is None:
        return None
    return query, memory_type, time_range


_TOOL_USAGE = {
    "status": "python -m pratyabhijna status",
    "bootstrap": "python -m pratyabhijna bootstrap",
    "inspect": "python -m pratyabhijna inspect UUID",
    "history": "python -m pratyabhijna history ENTITY_NAME",
    "recall": "python -m pratyabhijna recall QUERY [--type T] [--time-range R]",
}


def run_tool(config: PratyabhijnaConfig, action: str, argv: list[str]) -> int:
    """Dispatch read-only MCP tool subcommands.

    Validates args up front, then starts a ``PratyabhijnaService``
    for the call, invokes the tool handler directly, and prints the
    result as pretty JSON on stdout. Write tools (``remember``,
    ``correct``) are intentionally omitted — new memories are
    Vesper's decision, not an operator's.
    """
    import json

    import anyio

    # Validate args before paying the cost of starting the service.
    call: Callable[[Any], Awaitable[dict]] | None = None

    if action == "status":
        async def call(service):
            return {
                "version": "0.1.0",
                "db_connected": service.is_connected,
                **_cli_queue_stats(config.queue.db_path),
            }

    elif action == "bootstrap":
        from pratyabhijna.tools.bootstrap import bootstrap

        async def call(service):
            return await bootstrap(service)

    elif action == "inspect":
        if len(argv) != 1:
            print(f"Usage: {_TOOL_USAGE[action]}", file=sys.stderr)
            return 2
        from pratyabhijna.tools.inspect import inspect

        uuid = argv[0]

        async def call(service):
            return await inspect(service, uuid)

    elif action == "history":
        if len(argv) != 1:
            print(f"Usage: {_TOOL_USAGE[action]}", file=sys.stderr)
            return 2
        from pratyabhijna.tools.history import history

        entity_name = argv[0]

        async def call(service):
            return await history(service, entity_name)

    elif action == "recall":
        parsed = _parse_recall_args(argv)
        if parsed is None:
            print(f"Usage: {_TOOL_USAGE[action]}", file=sys.stderr)
            return 2
        query, memory_type, time_range = parsed
        from pratyabhijna.tools.recall import recall

        async def call(service):
            return await recall(
                service,
                query=query,
                memory_type=memory_type,
                time_range=time_range,
            )

    else:
        print(f"Unknown tool: {action}", file=sys.stderr)
        return 2

    async def _run() -> dict:
        from pratyabhijna.service import PratyabhijnaService

        service = PratyabhijnaService(config)
        await service.start()
        try:
            return await call(service)
        finally:
            await service.stop()

    result = anyio.run(_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


def main():
    config = PratyabhijnaConfig.from_env()
    configure_logging(config)

    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        sys.exit(run_seed(config, sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] in TOOL_COMMANDS:
        sys.exit(run_tool(config, sys.argv[1], sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "deadletters":
        sys.exit(run_deadletters(config, sys.argv[2:]))

    if not config.subject_name:
        print(
            "Pratyabhijna: subject_name is not configured. Set it in "
            "config/{env}.yaml or via PRATYABHIJNA_SUBJECT_NAME.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Build OAuth authorization server when server URL and api_key are both set.
    # The same api_key is the shared secret for /login — no separate password.
    oauth_provider = None
    oauth_storage = None
    auth = None
    if config.server.url and config.api_key:
        from mcp.server.auth.settings import (
            AuthSettings,
            ClientRegistrationOptions,
            RevocationOptions,
        )

        from pratyabhijna.oauth.provider import OAuthTTLs, PratyabhijnaOAuthProvider
        from pratyabhijna.oauth.storage import OAuthStorage

        oauth_storage = OAuthStorage(db_path=config.oauth.db_path)
        oauth_provider = PratyabhijnaOAuthProvider(
            storage=oauth_storage,
            ttls=OAuthTTLs(
                access_token=config.oauth.access_token_ttl_seconds,
                refresh_token=config.oauth.refresh_token_ttl_seconds,
                auth_code=config.oauth.auth_code_ttl_seconds,
                pending_session=config.oauth.pending_session_ttl_seconds,
            ),
            login_url_base=f"{config.server.url.rstrip('/')}/login",
        )
        auth = AuthSettings(
            issuer_url=config.server.url,
            resource_server_url=config.server.url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        )
        log.info("OAuth 2.1 authorization server enabled (issuer: %s)", config.server.url)
    elif config.server.url or config.api_key:
        log.warning(
            "Both PRATYABHIJNA_SERVER__URL and PRATYABHIJNA_API_KEY must be set to enable auth; "
            "running without authentication"
        )

    service = PratyabhijnaService(config)
    queue = WorkQueue(
        config.queue.db_path,
        config.queue.max_retries,
        config.queue.poll_interval,
        config.queue.backoff_base_seconds,
    )
    lifespan = build_lifespan(service, queue)
    # When running behind a reverse proxy (server.url set), disable FastMCP's
    # DNS rebinding protection — the proxy forwards Host: <external-domain>,
    # which the localhost allowlist would reject. OAuth covers us.
    from mcp.server.fastmcp.server import TransportSecuritySettings

    transport_security = (
        TransportSecuritySettings(enable_dns_rebinding_protection=False)
        if config.server.url
        else None  # let FastMCP auto-configure for localhost
    )

    server = create_server(
        service=service,
        queue=queue,
        subject_name=config.subject_name,
        lifespan=lifespan,
        auth_server_provider=oauth_provider,
        auth=auth,
        host="127.0.0.1",
        port=config.server.port,
        transport_security=transport_security,
    )

    if oauth_provider is not None:
        from pratyabhijna.oauth.login import register_login_routes

        register_login_routes(
            server=server,
            provider=oauth_provider,
            expected_password=config.api_key,
        )

    if config.server.url:
        log.info("Starting streamable-http transport on 127.0.0.1:%d", config.server.port)
        if oauth_provider is not None:
            # OAuth routes are hit before any MCP session exists, so OAuth
            # storage must start at ASGI startup (FastMCP's built-in lifespan
            # only runs per MCP session). build_http_app wraps the Starlette
            # app's lifespan accordingly; we then drive uvicorn directly.
            import asyncio

            import uvicorn

            from pratyabhijna.oauth import build_http_app

            starlette_app = build_http_app(server, oauth_storage)
            uvicorn_config = uvicorn.Config(
                starlette_app,
                host="127.0.0.1",
                port=config.server.port,
                log_level=config.log_level.lower(),
            )
            asyncio.run(uvicorn.Server(uvicorn_config).serve())
        else:
            server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
