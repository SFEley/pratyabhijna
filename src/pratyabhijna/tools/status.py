"""The `status` MCP tool.

Returns system orientation info: DB connection state,
queue depth, last write timestamp, server version,
synthesis state (last run, current delta count).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService


async def status(
    service: PratyabhijnaService | None = None,
    queue: WorkQueue | None = None,
) -> dict:
    """Return system health info.

    When service and queue are provided, returns live values.
    Otherwise returns stubs (backward compat with Phase 1).
    """
    if service is not None and queue is not None:
        dead = await queue.dead_letters()

        # Synthesis state — best-effort; None values when subject node absent.
        synthesis_last_run: str | None = None
        synthesis_delta_count: int | None = None
        try:
            from pratyabhijna.synthesis import get_identity_delta, get_subject_node
            node = await get_subject_node(service)
            if node is not None:
                synthesis_last_run = node.attributes.get("context_rebuilt_at")
                delta = await get_identity_delta(service, node)
                synthesis_delta_count = len(delta)
        except Exception:  # noqa: BLE001 — status should never raise
            pass

        return {
            "version": "0.1.0",
            "db_connected": service.is_connected,
            "queue_depth": await queue.depth(),
            "last_write": await queue.last_write(),
            "dead_letters": len(dead),
            "last_error": await queue.last_error(),
            "synthesis_last_run": synthesis_last_run,
            "synthesis_delta_count": synthesis_delta_count,
        }

    return {
        "version": "0.1.0",
        "db_connected": False,
        "queue_depth": 0,
        "last_write": None,
    }
