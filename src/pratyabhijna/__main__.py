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

    python -m pratyabhijna update DESCRIPTION [--cache]
                                                 Run a maintenance update (single)
                                                 (operator-only, off-MCP).
    python -m pratyabhijna update --input PATH   Run batched updates from audit JSON
                                                 (operator-only, off-MCP).
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


def _parse_update_args(argv: list[str]) -> tuple[str | None, bool, str | None] | None:
    """Parse ``update DESCRIPTION [--cache]`` or ``update --input PATH``.

    Returns (description, cache, input_path) or None on usage error.
    Exactly one of description or input_path must be provided.
    """
    description: str | None = None
    cache = False
    input_path: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--cache":
            cache = True
            i += 1
        elif a == "--input" and i + 1 < len(argv):
            input_path = argv[i + 1]
            i += 2
        elif not a.startswith("--") and description is None:
            description = a
            i += 1
        else:
            return None
    # Exactly one of description / input_path
    if (description is None) == (input_path is None):
        return None
    if description is not None and not description.strip():
        return None
    return description, cache, input_path


def run_update(config: PratyabhijnaConfig, argv: list[str]) -> int:
    """Run a maintenance update — single description or batched from audit JSON."""
    import anyio

    parsed = _parse_update_args(argv)
    if parsed is None:
        print(
            'Usage: python -m pratyabhijna update "DESCRIPTION" [--cache]\n'
            '   or: python -m pratyabhijna update --input PATH',
            file=sys.stderr,
        )
        return 2
    description, cache, input_path = parsed

    from pathlib import Path

    from pratyabhijna.output_log import write_output_file

    started_at = datetime.now(timezone.utc)

    if input_path is not None:
        # Batched-from-audit path
        from pratyabhijna.tools.update import update_from_audit_file

        async def _run() -> list[dict]:
            service = PratyabhijnaService(config)
            await service.start()
            try:
                return await update_from_audit_file(input_path, service=service)
            finally:
                await service.stop()

        try:
            results = anyio.run(_run)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log.exception("update --input failed before any results")
            results = [{
                "status": "Error",
                "request": f"--input {input_path}",
                "response": err,
                "guids": None, "count": 0, "queries": [],
                "warnings": [], "errors": [err],
            }]
        completed_at = datetime.now(timezone.utc)

        output_path = write_output_file(
            log_dir=Path(config.log_dir),
            group_id=config.subject_name,
            started_at=started_at,
            completed_at=completed_at,
            cache_requested=False,
            updates=results,
        )

        # Tally
        counts: dict[str, int] = {"Updated": 0, "Deleted": 0, "Rejected": 0, "Error": 0}
        for r in results:
            s = r.get("status", "Error")
            counts[s] = counts.get(s, 0) + 1
        summary = (
            f"Update batch complete: {counts['Updated']} Updated, "
            f"{counts['Deleted']} Deleted, {counts['Rejected']} Rejected, "
            f"{counts['Error']} Errored [output: {output_path}]"
        )
        if counts["Error"] > 0:
            print(summary, file=sys.stderr)
            return 1
        print(summary)
        return 0

    # Existing single-description path (unchanged behavior)
    from pratyabhijna.tools.update import update as _update

    async def _run_single() -> dict:
        service = PratyabhijnaService(config)
        await service.start()
        try:
            return await _update(service, description, cache=cache)
        finally:
            await service.stop()

    try:
        result = anyio.run(_run_single)
    except Exception as exc:
        # Infra failure (service start, Neo4j down, API timeout) bubbles
        # past the inner update() try/finally. Convert to an Error record
        # so the audit JSON is always written.
        err = f"{type(exc).__name__}: {exc}"
        log.exception("update run failed before result was returned")
        result = {
            "status": "Error",
            "request": description,
            "response": err,
            "guids": None,
            "count": 0,
            "queries": [],
            "warnings": [],
            "errors": [err],
        }
    completed_at = datetime.now(timezone.utc)

    output_path = write_output_file(
        log_dir=Path(config.log_dir),
        group_id=config.subject_name,
        started_at=started_at,
        completed_at=completed_at,
        cache_requested=cache,
        updates=[result],
    )

    # One-line summary on stdout. Errors (non-zero exit) on Error/Rejected
    # so shell pipelines can branch on it.
    summary = f"{result['status']}: {result['response']} [output: {output_path}]"
    if result["status"] in ("Error", "Rejected"):
        print(summary, file=sys.stderr)
        return 1
    print(summary)
    return 0


def _parse_audit_args(
    argv: list[str],
) -> tuple[str, str | None, str | None] | None:
    """Parse ``audit INPUT [--guidance G] [--output PATH]``.

    Returns (input_str, guidance, output_path) or None on usage error.
    """
    input_str: str | None = None
    guidance: str | None = None
    output_path: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--guidance" and i + 1 < len(argv):
            guidance = argv[i + 1]
            i += 2
        elif a == "--output" and i + 1 < len(argv):
            output_path = argv[i + 1]
            i += 2
        elif not a.startswith("--") and input_str is None:
            input_str = a
            i += 1
        else:
            return None
    if input_str is None or not input_str.strip():
        return None
    return input_str, guidance, output_path


def run_audit(config: PratyabhijnaConfig, argv: list[str]) -> int:
    """Run a node audit batch via the audit sub-agent.

    Outputs a JSON file at ``{log_dir}/audit/audit-{ISO}.json`` and appends
    a one-section summary to ``{repo_path}/memory/AUDIT.md``.
    """
    import anyio

    parsed = _parse_audit_args(argv)
    if parsed is None:
        print(
            'Usage: python -m pratyabhijna audit "INPUT" [--guidance G] [--output PATH]',
            file=sys.stderr,
        )
        return 2
    input_str, guidance, output_path = parsed

    from pathlib import Path
    import json as _json

    import anthropic
    from pratyabhijna.audit_log import append_to_audit_md, write_audit_file
    from pratyabhijna.tools.audit import run_audit_run

    async def _run() -> dict:
        service = PratyabhijnaService(config)
        await service.start()
        queue = WorkQueue(db_path=config.queue.db_path)
        # Register a no-op for add_episode so queue.enqueue() succeeds; the
        # actual processing happens when the main MCP server runs.
        queue.register("add_episode", lambda _: None)
        await queue.start(run_worker=False)

        api_key = config.llm.api_key or None
        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=300.0)
        try:
            return await run_audit_run(
                service, queue, input_str,
                guidance=guidance, anthropic_client=client,
            )
        finally:
            await client.close()
            await queue.stop()
            await service.stop()

    try:
        summary = anyio.run(_run)
    except Exception as exc:
        # Infra failure (Neo4j down, API timeout, batch submission failure)
        # bubbles past run_audit_run's internal handling. Convert to a minimal
        # summary so the JSON file is still written and the operator gets a
        # diagnostic record.
        err = f"{type(exc).__name__}: {exc}"
        log.exception("audit run failed before result was returned")
        now_iso = datetime.now(timezone.utc).isoformat()
        summary = {
            "results": [{
                "uuid": "",
                "name": "",
                "status": "Error",
                "analysis": err,
                "error": err,
            }],
            "started_at": now_iso,
            "completed_at": now_iso,
            "cohort_size": 0,
            "audit_revision": 0,
            "model": config.llm.audit_model,
            "guidance": guidance,
        }

    # Compute count summary for AUDIT.md and stdout
    counts = {"Valid": 0, "Update": 0, "Unfixable": 0, "Error": 0}
    unfixable_details: list[dict] = []
    for r in summary["results"]:
        s = r.get("status", "Error")
        counts[s] = counts.get(s, 0) + 1
        if s == "Unfixable":
            unfixable_details.append({
                "uuid": r["uuid"],
                "name": r.get("name", ""),
                "analysis": r.get("analysis", ""),
            })

    # Write the JSON file. --output overrides the default location.
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json.dumps(
            {"run_metadata": {
                "started_at": summary["started_at"],
                "completed_at": summary["completed_at"],
                "audit_revision": summary["audit_revision"],
                "guidance": summary["guidance"],
                "model": summary["model"],
                "cohort_size": summary["cohort_size"],
            }, "results": summary["results"]},
            indent=2, default=str,
        ))
        json_path = out
    else:
        json_path = write_audit_file(
            log_dir=Path(config.log_dir),
            run_metadata={
                "started_at": summary["started_at"],
                "completed_at": summary["completed_at"],
                "audit_revision": summary["audit_revision"],
                "guidance": summary["guidance"],
                "model": summary["model"],
                "cohort_size": summary["cohort_size"],
            },
            results=summary["results"],
        )

    # Append AUDIT.md summary (only if repo_path is configured)
    if config.resources.repo_path:
        append_to_audit_md(
            repo_path=config.resources.repo_path,
            run_summary={
                "started_at": summary["started_at"],
                "valid": counts["Valid"],
                "update": counts["Update"],
                "unfixable": counts["Unfixable"],
                "errored": counts["Error"],
                "unfixable_details": unfixable_details,
            },
        )

    summary_line = (
        f"Audit complete: {counts['Valid']} Valid, {counts['Update']} Update, "
        f"{counts['Unfixable']} Unfixable, {counts['Error']} Errored "
        f"[output: {json_path}]"
    )
    if any(r.get("status") == "Error" for r in summary["results"]):
        print(summary_line, file=sys.stderr)
        return 1
    print(summary_line)
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

  update DESCRIPTION [--cache]    Run a maintenance update (single)
  update --input PATH             Run batched updates from an audit JSON file
                                  (operator-only, off-MCP)

  audit INPUT [--guidance G]      Run a node audit batch via the audit sub-agent
        [--output PATH]           (operator-only, off-MCP)

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

    if len(sys.argv) > 1 and sys.argv[1] == "update":
        sys.exit(run_update(config, sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "audit":
        sys.exit(run_audit(config, sys.argv[2:]))

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
