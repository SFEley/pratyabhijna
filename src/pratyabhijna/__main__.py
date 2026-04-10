"""Pratyabhijna MCP server entry point.

Usage:
    python -m pratyabhijna                       Start MCP server
    python -m pratyabhijna seed                  Seed subject identity
    python -m pratyabhijna deadletters list      Show dead-lettered tasks
    python -m pratyabhijna deadletters show ID   Show full detail for one
    python -m pratyabhijna deadletters retry ID  Reset to pending (or --all)
    python -m pratyabhijna deadletters purge ID  Delete permanently (or --all)
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

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
    registers queue handlers, and stops both on exit.
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


def run_seed(config: PratyabhijnaConfig) -> None:
    """Run the seed subcommand synchronously."""
    import anyio

    from pratyabhijna.seed import seed_subject

    async def _run():
        service = PratyabhijnaService(config)
        await service.start()
        try:
            result = await seed_subject(service)
            log.info("Seed result: %s", result)
        finally:
            await service.stop()

    anyio.run(_run)


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


def main():
    config = PratyabhijnaConfig.from_env()
    configure_logging(config)

    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        run_seed(config)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "deadletters":
        sys.exit(run_deadletters(config, sys.argv[2:]))

    # Build auth if server URL and API key are configured.
    token_verifier = None
    auth = None
    if config.server.url and config.api_key:
        from mcp.server.auth.settings import AuthSettings

        from pratyabhijna.auth import StaticTokenVerifier

        token_verifier = StaticTokenVerifier(config.api_key)
        auth = AuthSettings(
            issuer_url=config.server.url,
            resource_server_url=config.server.url,
        )
        log.info("Bearer token auth enabled (resource server: %s)", config.server.url)
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
    # which the localhost allowlist would reject. Bearer token auth covers us.
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
        token_verifier=token_verifier,
        auth=auth,
        host="127.0.0.1",
        port=config.server.port,
        transport_security=transport_security,
    )

    if config.server.url:
        log.info("Starting streamable-http transport on 127.0.0.1:%d", config.server.port)
        server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
