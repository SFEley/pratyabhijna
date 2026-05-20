"""The ``bootstrap`` MCP tool.

Returns the subject's slimmed identity payload — the *recognition*
artifacts only, not the full reference text — plus synthesis metadata
(``context_rebuilt_at``, ``subject_delta``) from the Person node.

Shape (PR3 reshape, v0.19.0):

- ``soul`` — full SOUL.md text (small, constitutional, always loaded)
- ``user`` — full USER.md text (small, who the subject is with)
- ``identity_digest`` — full IDENTITY_DIGEST.md text (Self-Portrait
  summary + Drives + Observed Tensions, composed by the synthesizer)
- ``threads_active`` — only the ``## Active Threads`` section of
  THREADS.md (Recently Resolved is fetched on demand)
- ``chronicle_index`` — full CHRONICLE_INDEX.md text (date-indexed
  one-line teasers; full entries via ``read_chronicle_range``)

Heavy tier prose (full IDENTITY.md, full CHRONICLE.md, the resolved
threads) is no longer inlined — fetch via the ``read_tier`` and
``read_chronicle_range`` tools when a session actually needs it. The
slimmed bootstrap is what makes recognition fire; the heavy reads exist
for the cases where a session needs the full prose afterward.

After loading identity state, bootstrap checks whether synthesis is
stale and schedules a run if so. This is the primary synthesis trigger
— it ensures synthesis happens within one session's lag of accumulating
enough identity changes, without requiring any special memory_type tag
on writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pratyabhijna.log import get_logger
from pratyabhijna.synthesis import (
    extract_active_threads,
    get_subject_delta,
    get_subject_node,
    is_stale,
    read_identity_files,
)

if TYPE_CHECKING:
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService

_log = get_logger(__name__)


# Files read straight off disk in addition to the five canonical tiers
# (``read_identity_files``). These are synthesizer-composed sibling
# artifacts in the same ``memory/`` directory; they didn't exist when
# IDENTITY_FILES was defined and aren't part of the bootstrap-update
# pass, so they live as a separate read here rather than being folded
# into the canonical map.
_DIGEST_FILENAME = "IDENTITY_DIGEST.md"
_CHRONICLE_INDEX_FILENAME = "CHRONICLE_INDEX.md"


def _read_memory_file(repo_path: str, filename: str) -> str | None:
    """Read ``{repo_path}/memory/{filename}`` or return ``None`` if absent."""
    if not repo_path:
        return None
    memory_dir = Path(repo_path).expanduser().resolve() / "memory"
    if not memory_dir.is_dir():
        return None
    fp = memory_dir / filename
    return fp.read_text(encoding="utf-8").strip() if fp.is_file() else None


async def bootstrap(
    service: PratyabhijnaService,
    queue: WorkQueue | None = None,
) -> dict:
    """Return the slimmed bootstrap payload (PR3 shape).

    Reads from the subject's repo (``config.resources.repo_path``):
    SOUL.md, USER.md, IDENTITY_DIGEST.md, CHRONICLE_INDEX.md, plus the
    ``## Active Threads`` section of THREADS.md. Heavy tier prose is not
    returned — fetch via ``read_tier`` / ``read_chronicle_range``.

    The five tier-shaped fields are always present in the response;
    values are ``None`` when the corresponding file is missing or
    ``repo_path`` is unset. When ``queue`` is provided, checks whether
    synthesis is stale and schedules a run immediately if so. The
    synthesizer runs in the background; bootstrap returns without
    waiting for it.
    """
    node = await get_subject_node(service)
    repo_path = service.config.resources.repo_path
    files = read_identity_files(repo_path)
    threads_text = files.get("threads") if files else None

    tiers = {
        "soul": files.get("soul") if files else None,
        "user": files.get("user") if files else None,
        "identity_digest": _read_memory_file(repo_path, _DIGEST_FILENAME),
        "threads_active": (
            extract_active_threads(threads_text) if threads_text else None
        ),
        "chronicle_index": _read_memory_file(
            repo_path, _CHRONICLE_INDEX_FILENAME
        ),
    }

    base = {"subject": service.config.subject_name, **tiers}

    _available_tools = {
        "remember": "Write a new memory — observation, fact, position, or identity item. Supports `saga` to group episodes into ordered sequences.",
        "correct": "Fix a prior memory that was wrong (not just outdated).",
        "recall": "Search memory by semantic + keyword + graph traversal.",
        "read_tier": "Fetch one tier file in full (soul/identity/user/threads/chronicle) — heavy prose the slimmed bootstrap no longer inlines.",
        "read_chronicle_range": "Fetch chronicle entries dated within [start_date, end_date] — full prose for a window the index only teases.",
        "history": "Temporal evolution of an entity or topic.",
        "inspect": "Detailed view of a specific memory node by UUID.",
        "status": "System health — queue depth, graph connection, synthesis state.",
        "communities": "List all graph communities or display a single community with its members.",
        "query": "Natural-language graph query via adaptive-thinking sub-agent.",
    }

    if node is None:
        return {
            **base,
            "context_rebuilt_at": None,
            "subject_delta": [],
            "available_tools": _available_tools,
            "message": (
                f"No Person node found for '{service.config.subject_name}'. "
                "The subject node must be created before bootstrap can "
                "return synthesis metadata or subject_delta."
            ),
        }

    subject_delta = await get_subject_delta(service, node)

    if queue is not None and await is_stale(node, service):
        delay = service.config.synthesis.rebuild_delay_hours
        run_at = datetime.now(timezone.utc) + timedelta(hours=delay)
        await queue.reschedule_or_enqueue("synthesize", {}, run_at=run_at)
        _log.info(
            "synthesis scheduled from bootstrap "
            "(subject_delta=%d, context_rebuilt_at=%s)",
            len(subject_delta),
            node.attributes.get("context_rebuilt_at", "never"),
        )

    return {
        **base,
        "context_rebuilt_at": node.attributes.get("context_rebuilt_at"),
        "subject_delta": subject_delta,
        "available_tools": _available_tools,
    }
