"""The `remember` MCP tool.

Queues an observation, fact, reasoning, or identity item for
background processing via Graphiti's add_episode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vesper.queue import WorkQueue
    from vesper.service import VesperService


async def remember(
    queue: WorkQueue,
    content: str,
    memory_type: str = "observation",
    source: str = "vesper",
) -> dict:
    """Enqueue a memory for background processing.

    Returns immediately with a task ID. The background worker
    calls graphiti.add_episode() to extract entities, embed,
    and store.
    """
    task_id = await queue.enqueue(
        "add_episode",
        {
            "content": content,
            "memory_type": memory_type,
            "source": source,
        },
    )
    return {"task_id": task_id, "status": "queued"}


def make_handler(service: VesperService):
    """Create the add_episode queue handler bound to a service instance."""

    async def handle_add_episode(payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        await service._graphiti.add_episode(
            name=f"{payload['memory_type']}:{now.isoformat()}",
            episode_body=payload["content"],
            source_description=payload["source"],
            reference_time=now,
            entity_types=service.entity_types,
        )
        # TODO Phase 5: if memory_type == "identity", mark synthesis stale

    return handle_add_episode
