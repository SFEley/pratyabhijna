"""The `status` MCP tool.

Returns system orientation as a nested dict with three blocks:

- ``queue`` — pending/running/completed/dead-letter counts, plus a
  per-task-type breakdown. Read from SQLite directly via
  ``collect_queue_stats`` so the CLI and MCP paths surface identical data
  without instantiating a second ``WorkQueue``.
- ``graph`` — node and edge counts (total and by label/type), plus a
  supersession count (edges with ``invalid_at`` set).
- ``synthesis`` — when the context was last rebuilt and the current delta
  size.

Plus top-level ``version``, ``db_connected``, and ``subject_name``.

Status should never raise. Each collector catches its own failures and
surfaces ``None`` / ``0`` for degraded metrics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pratyabhijna.log import get_logger
from pratyabhijna.queue import collect_queue_stats

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService

_log = get_logger(__name__)

_VERSION = "0.1.0"


async def status(
    service: PratyabhijnaService | None = None,
    queue_db_path: str | None = None,
) -> dict:
    """Return system health info as a nested dict.

    Stub path (``service is None``) returns a minimal dict for Phase 1
    backward compatibility. Wired path assembles all three blocks.
    """
    if service is None:
        # Shape-invariant stub so destructuring consumers don't KeyError.
        return {
            "version": _VERSION,
            "db_connected": False,
            "subject_name": None,
            "queue": _collect_queue(None),
            "graph": {
                "nodes_total": None, "nodes_by_label": {},
                "edges_total": None, "edges_by_type": {},
                "supersessions": None,
            },
            "synthesis": {"last_run": None, "delta_count": None},
        }

    return {
        "version": _VERSION,
        "db_connected": service.is_connected,
        "subject_name": service.config.subject_name,
        "queue": _collect_queue(queue_db_path),
        "graph": await _collect_graph(service),
        "synthesis": await _collect_synthesis(service),
    }


def _collect_queue(db_path: str | None) -> dict:
    if db_path is None:
        return {
            "depth": 0,
            "last_write": None,
            "dead_letters": 0,
            "last_error": None,
            "by_task_type": {},
        }
    try:
        return collect_queue_stats(db_path)
    except Exception:  # noqa: BLE001 — status should never raise
        _log.warning("collect_queue_stats failed", exc_info=True)
        return {
            "depth": None,
            "last_write": None,
            "dead_letters": None,
            "last_error": None,
            "by_task_type": {},
        }


async def _collect_graph(service: PratyabhijnaService) -> dict:
    """Run the five graph counts; each failure degrades to None/empty."""
    return {
        "nodes_total": await _safe(service.count_nodes_total),
        "nodes_by_label": await _safe(service.count_nodes_by_label, default={}),
        "edges_total": await _safe(service.count_edges_total),
        "edges_by_type": await _safe(service.count_edges_by_type, default={}),
        "supersessions": await _safe(service.count_supersessions),
    }


async def _collect_synthesis(service: PratyabhijnaService) -> dict:
    """Subject-node driven synthesis stats."""
    last_run = None
    delta_count = None
    try:
        from pratyabhijna.synthesis import get_identity_delta, get_subject_node

        node = await get_subject_node(service)
        if node is not None:
            last_run = node.attributes.get("context_rebuilt_at")
            delta = await get_identity_delta(service, node)
            delta_count = len(delta)
    except Exception:  # noqa: BLE001
        _log.warning("collect_synthesis failed", exc_info=True)
    return {"last_run": last_run, "delta_count": delta_count}


async def _safe(coro_fn, default=None):
    """Call an async count method; return default on failure."""
    try:
        return await coro_fn()
    except Exception:  # noqa: BLE001
        _log.warning("status: %s failed", getattr(coro_fn, "__name__", "?"), exc_info=True)
        return default
