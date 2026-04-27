"""The `correct` MCP tool.

Queues a correction for background processing. Graphiti's bi-temporal
model handles edge invalidation (invalid_at on contradicted edges)
when the correction episode is processed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from pratyabhijna.log import get_logger
from pratyabhijna.synthesis import IDENTITY_LABELS

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


def make_handler(service: PratyabhijnaService, queue: WorkQueue | None = None):
    """Create the correct_memory queue handler bound to a service instance.

    When ``queue`` is provided, the handler schedules a singleton
    synthesis task whenever the subject Person node has any neighbor
    with an identity label (Observation, Drive, Question).
    This is intentionally broad — the singleton kick-forward in
    ``reschedule_or_enqueue`` collapses repeated triggers into one run,
    while under-triggering would silently skip real identity changes.

    Corrections made when no identity neighbors exist do not trigger.
    """

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
            group_id=service.config.subject_name,
            entity_types=service.entity_types,
            **({"custom_extraction_instructions": extraction_hint}
               if extraction_hint else {}),
        )
        _log.info("add_episode complete (type=correction)")

        if queue is None:
            return

        if await _subject_has_identity_neighbor(service):
            delay = service.config.synthesis.rebuild_delay_hours
            run_at = datetime.now(timezone.utc) + timedelta(hours=delay)
            await queue.reschedule_or_enqueue("synthesize", {}, run_at=run_at)
            _log.info("synthesis scheduled from correction (run_at=%s)", run_at.isoformat())

    return handle_correct_memory


async def _subject_has_identity_neighbor(
    service: PratyabhijnaService,
) -> bool:
    """Whether the subject Person node has any neighbor with an identity label.

    Intended to be called after add_episode to detect whether this
    correction plausibly touched identity. A blunt heuristic: if the
    subject has any identity-typed neighbor at all, we schedule
    synthesis. The singleton kick-forward means this is safe to be
    permissive about.
    """
    subject = await service.get_entity_by_name(service.config.subject_name)
    if subject is None:
        return False
    try:
        edges = await service.get_edges_for_node(subject.uuid)
    except Exception:  # noqa: BLE001 — graph hiccup shouldn't tank the write
        _log.warning("failed to fetch edges for identity-touch check", exc_info=True)
        return False

    for edge in edges:
        other_uuid = (
            edge.target_node_uuid
            if edge.source_node_uuid == subject.uuid
            else edge.source_node_uuid
        )
        try:
            other = await service.get_entity_by_uuid(other_uuid)
        except Exception:  # noqa: BLE001
            continue
        if set(getattr(other, "labels", []) or []) & IDENTITY_LABELS:
            return True
    return False
