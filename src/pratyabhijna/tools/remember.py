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
    saga: str | None = None,
    saga_previous_episode_uuid: str | None = None,
) -> dict:
    """Enqueue a memory for background processing.

    Returns immediately with a task ID. The background worker
    calls graphiti.add_episode() to extract entities, embed,
    and store.

    occurred_at: ISO-8601 timestamp for when the fact was true in
    the world (Graphiti's `reference_time`). Defaults to now.

    saga: optional saga name to group this episode into an ordered
    sequence. Pass saga_previous_episode_uuid to chain it after a
    specific prior episode in the same saga.
    """
    payload: dict[str, Any] = {
        "content": content,
        "memory_type": memory_type,
        "source": source,
        "occurred_at": occurred_at,
    }
    if saga is not None:
        payload["saga"] = saga
    if saga_previous_episode_uuid is not None:
        payload["saga_previous_episode_uuid"] = saga_previous_episode_uuid

    task_id = await queue.enqueue("add_episode", payload)
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


def make_handler(service: PratyabhijnaService, queue: WorkQueue | None = None):
    """Create the add_episode queue handler bound to a service instance.

    ``queue`` is accepted for interface compatibility but is no longer
    used here — synthesis is scheduled from the bootstrap tool, not from
    individual write handlers. Pass ``None`` in contexts (e.g. seeding)
    where triggering synthesis isn't desired.
    """

    async def handle_add_episode(payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        reference_time = _resolve_reference_time(payload.get("occurred_at"))
        saga = payload.get("saga")
        saga_prev = payload.get("saga_previous_episode_uuid")
        _log.info(
            "add_episode starting (type=%s, len=%d, reference_time=%s%s, pipeline=%s)",
            payload["memory_type"],
            len(payload["content"]),
            reference_time.isoformat(),
            f", saga={saga}" if saga else "",
            "in-house" if service.config.add_episode.use_in_house else "graphiti",
        )

        if service.config.add_episode.use_in_house:
            from pratyabhijna.add_episode import add_episode as in_house_add_episode
            await in_house_add_episode(
                service,
                name=f"{payload['memory_type']}:{now.isoformat()}",
                episode_body=payload["content"],
                source_description=payload["source"],
                reference_time=reference_time,
                group_id=service.config.subject_name,
                saga=saga,
                saga_previous_episode_uuid=saga_prev,
            )
        else:
            await service._graphiti.add_episode(
                name=f"{payload['memory_type']}:{now.isoformat()}",
                episode_body=payload["content"],
                source_description=payload["source"],
                reference_time=reference_time,
                group_id=service.config.subject_name,
                entity_types=service.entity_types,
                saga=saga,
                saga_previous_episode_uuid=saga_prev,
            )
        _log.info("add_episode complete (type=%s)", payload["memory_type"])

    return handle_add_episode
