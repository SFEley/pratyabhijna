"""The `correct` MCP tool.

Queues a correction for background processing. Graphiti's bi-temporal
model handles edge invalidation (invalid_at on contradicted edges)
when the correction episode is processed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService


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


def make_handler(service: PratyabhijnaService):
    """Create the correct_memory queue handler bound to a service instance."""

    async def handle_correct_memory(payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        search_terms = payload.get("search_terms", "")

        # Build extraction hint so Graphiti focuses on the right entities.
        # Without this, a generic correction like "X is actually Y" might
        # not resolve to the intended nodes.
        extraction_hint = (
            f"Focus entity extraction on: {search_terms}. "
            "This is a correction — look for existing entities matching "
            "these terms and update or invalidate contradicted edges."
        ) if search_terms else None

        await service._graphiti.add_episode(
            name=f"correction:{now.isoformat()}",
            episode_body=payload["content"],
            source_description="correction",
            reference_time=now,
            entity_types=service.entity_types,
            **({"custom_extraction_instructions": extraction_hint}
               if extraction_hint else {}),
        )
        # TODO Phase 5: if correction touches identity entities, mark synthesis stale

    return handle_correct_memory
