"""The ``read_tier`` MCP tool (bootstrap-redesign PR2).

Fetches one identity tier file in full, on demand. This is the prose
brain's read path for the heavy tiers the slimmed bootstrap (PR3) will
no longer inline — reviewing a thread in detail, quoting chronicle,
working with the full Self-Portrait. ``recall`` is graph-side
associational retrieval; this is a plain file read, a different need.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pratyabhijna.synthesis import IDENTITY_FILES

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService


async def read_tier(service: PratyabhijnaService, name: str) -> dict:
    """Return one identity tier file in full, plus a freshness stamp.

    Args:
        service: The PratyabhijnaService instance.
        name: Tier name — one of soul/identity/user/threads/chronicle.

    Returns:
        ``{"tier", "text", "freshness"}`` where ``freshness`` is the
        file's mtime (ISO-8601, UTC). Error dict for an unknown tier
        name or a missing file/repo, never an exception.
    """
    filename = IDENTITY_FILES.get(name)
    if filename is None:
        return {
            "error": "invalid_tier",
            "message": (
                f"Unknown tier '{name}'. "
                f"Valid: {', '.join(sorted(IDENTITY_FILES))}."
            ),
        }
    repo_path = service.config.resources.repo_path
    if not repo_path:
        return {
            "error": "not_found",
            "message": "config.resources.repo_path is not set.",
        }
    filepath = Path(repo_path).expanduser().resolve() / "memory" / filename
    if not filepath.is_file():
        return {
            "error": "not_found",
            "message": f"Tier file for '{name}' not found at {filepath}.",
        }
    text = filepath.read_text(encoding="utf-8")
    mtime = datetime.fromtimestamp(
        filepath.stat().st_mtime, tz=timezone.utc
    ).isoformat()
    return {"tier": name, "text": text, "freshness": mtime}
