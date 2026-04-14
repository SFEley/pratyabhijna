"""The `correct` MCP tool.

Queues a correction for background processing. Graphiti's bi-temporal
model handles edge invalidation (invalid_at on contradicted edges)
when the correction episode is processed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pratyabhijna.log import get_logger

if TYPE_CHECKING:
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService

_log = get_logger(__name__)


async def correct(
    queue: WorkQueue,
    content: str,
    search_terms: str,
    occurred_at: str | None = None,
) -> dict:
    """Enqueue a correction for background processing.

    Returns immediately with a task ID. The background worker
    stores the correction as an episode — Graphiti handles
    edge invalidation internally.

    occurred_at: ISO-8601 timestamp for when the corrected fact was
    true in the world (Graphiti's `reference_time`). Defaults to now.
    Use when correcting a historical fact whose occurrence date
    differs from the moment of correction.
    """
    task_id = await queue.enqueue(
        "correct_memory",
        {
            "content": content,
            "search_terms": search_terms,
            "occurred_at": occurred_at,
        },
    )
    return {"task_id": task_id, "status": "queued"}


def _resolve_reference_time(occurred_at: str | None) -> datetime:
    if not occurred_at:
        return datetime.now(timezone.utc)
    ts = occurred_at.replace("Z", "+00:00") if occurred_at.endswith("Z") else occurred_at
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def make_handler(service: PratyabhijnaService):
    """Create the correct_memory queue handler bound to a service instance."""

    async def handle_correct_memory(payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        reference_time = _resolve_reference_time(payload.get("occurred_at"))
        search_terms = payload.get("search_terms", "")

        # Build extraction hint so Graphiti focuses on the right entities.
        # Without this, a generic correction like "X is actually Y" might
        # not resolve to the intended nodes.
        extraction_hint = (
            f"Focus entity extraction on: {search_terms}. "
            "This is a correction — look for existing entities matching "
            "these terms and update or invalidate contradicted edges."
        ) if search_terms else None

        _log.info(
            "add_episode starting (type=correction, len=%d)", len(payload["content"])
        )
        await service._graphiti.add_episode(
            name=f"correction:{now.isoformat()}",
            episode_body=payload["content"],
            source_description="correction",
            reference_time=reference_time,
            entity_types=service.entity_types,
            **({"custom_extraction_instructions": extraction_hint}
               if extraction_hint else {}),
        )
        _log.info("add_episode complete (type=correction)")
        # TODO Phase 5: if correction touches identity entities, mark synthesis stale

    return handle_correct_memory
