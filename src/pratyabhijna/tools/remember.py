"""The `remember` MCP tool.

Queues an observation, fact, reasoning, or identity item for
background processing via Graphiti's add_episode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pratyabhijna.log import get_logger

if TYPE_CHECKING:
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService

_log = get_logger(__name__)


async def remember(
    queue: WorkQueue,
    content: str,
    memory_type: str = "observation",
    source: str = "self",
    occurred_at: str | None = None,
) -> dict:
    """Enqueue a memory for background processing.

    Returns immediately with a task ID. The background worker
    calls graphiti.add_episode() to extract entities, embed,
    and store.

    occurred_at: ISO-8601 timestamp for when the fact was true in
    the world (Graphiti's `reference_time`). Defaults to now.
    """
    task_id = await queue.enqueue(
        "add_episode",
        {
            "content": content,
            "memory_type": memory_type,
            "source": source,
            "occurred_at": occurred_at,
        },
    )
    return {"task_id": task_id, "status": "queued"}


def _resolve_reference_time(occurred_at: str | None) -> datetime:
    if not occurred_at:
        return datetime.now(timezone.utc)
    # fromisoformat in 3.11+ accepts trailing 'Z', but be defensive.
    ts = occurred_at.replace("Z", "+00:00") if occurred_at.endswith("Z") else occurred_at
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def make_handler(service: PratyabhijnaService):
    """Create the add_episode queue handler bound to a service instance."""

    async def handle_add_episode(payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        reference_time = _resolve_reference_time(payload.get("occurred_at"))
        _log.info(
            "add_episode starting (type=%s, len=%d, reference_time=%s)",
            payload["memory_type"],
            len(payload["content"]),
            reference_time.isoformat(),
        )
        await service._graphiti.add_episode(
            name=f"{payload['memory_type']}:{now.isoformat()}",
            episode_body=payload["content"],
            source_description=payload["source"],
            reference_time=reference_time,
            entity_types=service.entity_types,
        )
        _log.info("add_episode complete (type=%s)", payload["memory_type"])
        # TODO Phase 5: if memory_type == "identity", mark synthesis stale

    return handle_add_episode
