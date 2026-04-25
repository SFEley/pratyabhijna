"""Pratyabhijna MCP server entry point.

Usage:
    python -m pratyabhijna                       Start MCP server
    python -m pratyabhijna seed [--name NAME] [--soul-file PATH] [--identity-file PATH]
                                                 Seed subject identity

    python -m pratyabhijna synthesis [MINUTES]   Enqueue synthesis run (default: now)

    python -m pratyabhijna status                System orientation
    python -m pratyabhijna bootstrap             Subject identity tiers
    python -m pratyabhijna inspect UUID          Node or edge detail
    python -m pratyabhijna history ENTITY        Entity relationship timeline
    python -m pratyabhijna recall QUERY [--type T] [--time-range R] [--limit N]

    python -m pratyabhijna deadletters list      Show dead-lettered tasks
    python -m pratyabhijna deadletters show ID   Show full detail for one
    python -m pratyabhijna deadletters retry ID  Reset to pending (or --all)
    python -m pratyabhijna deadletters purge ID  Delete permanently (or --all)
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.log import configure_logging, get_logger
from pratyabhijna.queue import WorkQueue
from pratyabhijna.server import create_server
from pratyabhijna.service import PratyabhijnaService
from pratyabhijna.synthesis_agent import make_synthesize_handler
from pratyabhijna.tools.correct import make_handler as correct_make_handler
from pratyabhijna.tools.remember import make_handler as remember_make_handler

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = get_logger(__name__)


def build_lifespan(service: PratyabhijnaService, queue: WorkQueue):
    """Create the async lifespan context manager for the MCP server.

    Handlers must be registered before this lifespan is used (call
    ``register_queue_handlers`` after constructing the queue). Service and
    queue lifecycle (start/stop, crash recovery) are managed by the
    ASGI-level lifespan in ``oauth.build_http_app``, which runs at uvicorn
    startup before any MCP session exists. FastMCP's lifespan fires per MCP
    session, so it cannot own start/stop — the queue worker would not run
    until the first client connected, and crashed tasks would not be
    recovered on restart.
    """

    @asynccontextmanager
    async def lifespan(app: FastMCP):
        log.info("Pratyabhijna MCP session ready")
        yield

    return lifespan


def register_queue_handlers(
    service: PratyabhijnaService, queue: WorkQueue
) -> None:
    """Register all task handlers on the queue before it starts.

    Must be called before ``queue.start()`` so that crash-recovered tasks
    dispatched by the worker loop always find their handlers present.
    Synthesize is registered first because the remember/correct handlers
    call ``reschedule_or_enqueue("synthesize", ...)`` and that method
    validates the handler exists before inserting.
    """
    queue.register("synthesize", make_synthesize_handler(service, service.config))
    queue.register("add_episode", remember_make_handler(service, queue=queue))
    queue.register("correct_memory", correct_make_handler(service, queue=queue))


def _parse_seed_args(argv: list[str]) -> str | None | False:
    """Parse ``seed [--name NAME]``.

    Returns the name (or None if absent), or False on usage error.
    """
    name: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--name" and i + 1 < len(argv):
            name = argv[i + 1]
            i += 2
        else:
            return False
    return name


def run_seed(config: PratyabhijnaConfig, argv: list[str]) -> int:
    """Run the seed subcommand synchronously.

    Creates the subject's Person node in the graph. Tier text is no
    longer stored on the node — files in the subject's repo are the
    canonical source. This command now just anchors the identity.
    """
    import anyio

    from pratyabhijna.seed import seed_subject

    parsed = _parse_seed_args(argv)
    if parsed is False:
        print(
            "Usage: python -m pratyabhijna seed [--name NAME]",
            file=sys.stderr,
        )
        return 2
    cli_name = parsed

    subject_name = cli_name or config.subject_name
    if not subject_name:
        print(
            "seed: missing subject name (--name or config subject_name)",
            file=sys.stderr,
        )
        return 2
    if cli_name:
        config.subject_name = cli_name

    async def _run():
        service = PratyabhijnaService(config)
        await service.start()
        try:
            result = await seed_subject(service)
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


def _parse_recall_args(
    argv: list[str],
) -> tuple[str, str | None, str | None, int] | None:
    """Parse ``recall QUERY [--type T] [--time-range R] [--limit N]``.

    Returns (query, memory_type, time_range, limit) or None on usage error.
    Default limit is 5.
    """
    query: str | None = None
    memory_type: str | None = None
    time_range: str | None = None
    limit: int = 5
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--type" and i + 1 < len(argv):
            memory_type = argv[i + 1]
            i += 2
        elif a == "--time-range" and i + 1 < len(argv):
            time_range = argv[i + 1]
            i += 2
        elif a == "--limit" and i + 1 < len(argv):
            try:
                limit = int(argv[i + 1])
            except ValueError:
                return None
            i += 2
        elif not a.startswith("--") and query is None:
            query = a
            i += 1
        else:
            return None
    if query is None:
        return None
    return query, memory_type, time_range, limit


_TOOL_USAGE = {
    "status": "python -m pratyabhijna status",
    "bootstrap": "python -m pratyabhijna bootstrap",
    "inspect": "python -m pratyabhijna inspect UUID",
    "history": "python -m pratyabhijna history ENTITY_NAME",
    "recall": "python -m pratyabhijna recall QUERY [--type T] [--time-range R] [--limit N]",
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
        from pratyabhijna.tools.status import status as _status

        async def call(service):
            return await _status(
                service=service,
                queue_db_path=config.queue.db_path,
            )

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
        query, memory_type, time_range, limit = parsed
        from pratyabhijna.tools.recall import recall

        async def call(service):
            return await recall(
                service,
                query=query,
                memory_type=memory_type,
                time_range=time_range,
                limit=limit,
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


def run_synthesis_cmd(config: PratyabhijnaConfig, argv: list[str]) -> int:
    """Enqueue a synthesis task, optionally delayed by N minutes."""
    import anyio

    delay_minutes = 0
    if argv:
        try:
            delay_minutes = int(argv[0])
            if delay_minutes < 0:
                raise ValueError
        except ValueError:
            print(
                "Usage: python -m pratyabhijna synthesis [MINUTES]",
                file=sys.stderr,
            )
            return 2

    async def _run():
        queue = WorkQueue(db_path=config.queue.db_path)
        queue.register("synthesize", lambda _: None)
        await queue.start(run_worker=False)
        try:
            run_at = datetime.now(timezone.utc)
            if delay_minutes:
                run_at += timedelta(minutes=delay_minutes)
            task_id = await queue.reschedule_or_enqueue("synthesize", {}, run_at=run_at)
            if delay_minutes:
                print(f"Synthesis scheduled in {delay_minutes}m (task {task_id})")
            else:
                print(f"Synthesis queued (task {task_id})")
        finally:
            await queue.stop()

    anyio.run(_run)
    return 0


_HELP = """\
Usage: python -m pratyabhijna [COMMAND]

Commands:
  (none)                          Start MCP server
  help                            Show this message

  seed [--name NAME]              Seed subject identity node

  synthesis [MINUTES]             Enqueue a synthesis run (default: now)

  status                          System orientation (queue, graph, synthesis)
  bootstrap                       Subject identity tiers
  inspect UUID                    Node or edge detail
  history ENTITY                  Entity relationship timeline
  recall QUERY [--type T] [--limit N]  Graph search (default limit: 5)
              [--time-range R]

  deadletters list                Show dead-lettered tasks
  deadletters show ID             Full detail for one task
  deadletters retry (ID|--all)    Reset to pending
  deadletters purge (ID|--all)    Delete permanently
"""


def main():
    config = PratyabhijnaConfig.from_env()
    configure_logging(config)

    if len(sys.argv) > 1 and sys.argv[1] == "help":
        print(_HELP, end="")
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        sys.exit(run_seed(config, sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "synthesis":
        sys.exit(run_synthesis_cmd(config, sys.argv[2:]))

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
    register_queue_handlers(service, queue)
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
        repo_path=config.resources.repo_path,
        resource_directories=config.resources.directories,
    )

    if oauth_provider is not None:
        from pratyabhijna.oauth.login import register_login_routes

        register_login_routes(
            server=server,
            provider=oauth_provider,
            expected_password=config.api_key,
        )

    if config.server.url:
        # Always drive uvicorn directly so service/queue startup happens at
        # ASGI startup (before any MCP session). FastMCP's built-in lifespan
        # only fires per MCP session, so queue crash recovery would not run
        # until the first client connected if we relied on server.run().
        import asyncio

        import uvicorn

        from pratyabhijna.oauth import build_http_app

        log.info("Starting streamable-http transport on 127.0.0.1:%d", config.server.port)
        starlette_app = build_http_app(server, service, queue, oauth_storage)
        uvicorn_config = uvicorn.Config(
            starlette_app,
            host="127.0.0.1",
            port=config.server.port,
            log_level=config.log_level.lower(),
        )
        asyncio.run(uvicorn.Server(uvicorn_config).serve())
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
