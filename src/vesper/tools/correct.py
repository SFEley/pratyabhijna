"""The `correct` MCP tool.

Queues a correction for background processing. Graphiti's bi-temporal
model handles edge invalidation (invalid_at on contradicted edges)
when the correction episode is processed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vesper.queue import WorkQueue
    from vesper.service import VesperService


async def correct(
    queue: WorkQueue,
    content: str,
    search_terms: str,
) -> dict:
    """Enqueue a correction for background processing.

    Returns immediately with a task ID. The background worker
    stores the correction as an episode — Graphiti handles
    edge invalidation internally.
    """
    task_id = await queue.enqueue(
        "correct_memory",
        {
            "content": content,
            "search_terms": search_terms,
        },
    )
    return {"task_id": task_id, "status": "queued"}


def make_handler(service: VesperService):
    """Create the correct_memory queue handler bound to a service instance."""

    async def handle_correct_memory(payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        # Store correction as an episode. Graphiti's entity extraction
        # will detect contradictions and set invalid_at on superseded edges.
        await service._graphiti.add_episode(
            name=f"correction:{now.isoformat()}",
            episode_body=payload["content"],
            source_description="correction",
            reference_time=now,
            entity_types=service.entity_types,
        )
        # TODO Phase 5: if correction touches identity entities, mark synthesis stale

    return handle_correct_memory
