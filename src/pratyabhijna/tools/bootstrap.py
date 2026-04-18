"""The ``bootstrap`` MCP tool.

Returns the subject's identity tiers from repo files plus synthesis
metadata (context_rebuilt_at, delta) from the Person node.

The tier text itself lives only in the subject's repo — the graph no
longer duplicates SOUL/IDENTITY/USER/THREADS/CHRONICLE as Person-node
attributes. Files are canonical. The Person node carries synthesis
metadata and anchors identity-atom edges.

After loading identity state, bootstrap checks whether synthesis is
stale and schedules a run if so. This is the primary synthesis trigger
— it ensures synthesis happens within one session's lag of accumulating
enough identity changes, without requiring any special memory_type tag
on writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pratyabhijna.log import get_logger
from pratyabhijna.synthesis import (
    IDENTITY_FILES,
    get_identity_delta,
    get_subject_node,
    is_stale,
    read_identity_files,
)

if TYPE_CHECKING:
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService

_log = get_logger(__name__)


async def bootstrap(
    service: PratyabhijnaService,
    queue: WorkQueue | None = None,
) -> dict:
    """Return the subject's bootstrap tiers and identity delta.

    Reads identity files from the repo (``config.resources.repo_path``)
    and combines them with synthesis metadata from the Person node. All
    five tier fields (``soul``, ``identity``, ``user``, ``threads``,
    ``chronicle``) are always present in the response; values are None
    when the corresponding file is missing or ``repo_path`` is unset.

    When ``queue`` is provided, checks whether synthesis is stale and
    schedules a run immediately if so. The synthesizer runs in the
    background; bootstrap returns without waiting for it.
    """
    node = await get_subject_node(service)
    files = read_identity_files(service.config.resources.repo_path)
    tiers = {key: (files.get(key) if files else None) for key in IDENTITY_FILES}

    base = {"subject": service.config.subject_name, **tiers}

    _available_tools = {
        "remember": "Write a new memory — observation, fact, position, or identity item. Supports `saga` to group episodes into ordered sequences.",
        "correct": "Fix a prior memory that was wrong (not just outdated).",
        "recall": "Search memory by semantic + keyword + graph traversal.",
        "history": "Temporal evolution of an entity or topic.",
        "inspect": "Detailed view of a specific memory node by UUID.",
        "status": "System health — queue depth, graph connection, synthesis state.",
    }

    if node is None:
        return {
            **base,
            "context_rebuilt_at": None,
            "delta": [],
            "available_tools": _available_tools,
            "message": (
                f"No Person node found for '{service.config.subject_name}'. "
                "The subject node must be created before bootstrap can "
                "return synthesis metadata or delta."
            ),
        }

    delta = await get_identity_delta(service, node)

    if queue is not None and await is_stale(node, service):
        delay = service.config.synthesis.rebuild_delay_hours
        run_at = datetime.now(timezone.utc) + timedelta(hours=delay)
        await queue.reschedule_or_enqueue("synthesize", {}, run_at=run_at)
        _log.info(
            "synthesis scheduled from bootstrap (delta=%d, context_rebuilt_at=%s)",
            len(delta),
            node.attributes.get("context_rebuilt_at", "never"),
        )

    return {
        **base,
        "context_rebuilt_at": node.attributes.get("context_rebuilt_at"),
        "delta": delta,
        "available_tools": _available_tools,
    }
