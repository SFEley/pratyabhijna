"""The ``read_chronicle_range`` MCP tool (bootstrap-redesign PR2).

Fetches chronicle entries whose date falls in ``[start_date, end_date]``
— the "what was happening last week / last month" need. Reuses
``parse_chronicle_entries`` (and its date parsing) so heading-date
formats stay consistent with Pass 2 and the chronicle index. A file
read, distinct from ``recall``'s graph-side retrieval.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from pratyabhijna.synthesis import parse_chronicle_entries

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService


async def read_chronicle_range(
    service: PratyabhijnaService,
    start_date: str,
    end_date: str,
) -> dict:
    """Return chronicle entries dated within ``[start_date, end_date]``.

    Args:
        service: The PratyabhijnaService instance.
        start_date / end_date: inclusive ISO-8601 dates (``YYYY-MM-DD``).

    Returns:
        ``{"start", "end", "count", "entries", "undated_skipped"}``.
        ``entries`` is chronological (oldest first); each is
        ``{date, heading, title, text}``. ``undated_skipped`` counts
        entries whose heading date couldn't be parsed (so a caller knows
        the window result may be incomplete — surfaced, not silent).
        Error dict for malformed dates, an inverted range, or a missing
        chronicle — never an exception.
    """
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as e:
        return {
            "error": "invalid_date",
            "message": f"Dates must be ISO-8601 (YYYY-MM-DD): {e}",
        }
    if start > end:
        return {
            "error": "invalid_range",
            "message": f"start_date {start_date} is after end_date {end_date}.",
        }

    repo_path = service.config.resources.repo_path
    if not repo_path:
        return {
            "error": "not_found",
            "message": "config.resources.repo_path is not set.",
        }
    filepath = Path(repo_path).expanduser().resolve() / "memory" / "CHRONICLE.md"
    if not filepath.is_file():
        return {
            "error": "not_found",
            "message": f"CHRONICLE.md not found at {filepath}.",
        }

    entries = parse_chronicle_entries(filepath.read_text(encoding="utf-8"))
    matched = []
    undated = 0
    for e in entries:
        if e.parsed_date is None:
            undated += 1
            continue
        if start <= e.parsed_date <= end:
            matched.append({
                "date": e.parsed_date.isoformat(),
                "heading": e.heading,
                "title": e.title,
                "text": e.full_block.rstrip("\n"),
            })
    matched.sort(key=lambda m: m["date"])
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(matched),
        "entries": matched,
        "undated_skipped": undated,
    }
