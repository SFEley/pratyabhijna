"""JSON output log for ``pratyabhijna update`` runs.

Each invocation of the update CLI writes a JSON file at
``{log_dir}/outputs/output-{ISO8601-UTC}.json`` capturing what the
operator asked for, what the agent decided, what Cypher ran, and any
warnings or errors. The schema is post-mortem-oriented: the goal is
that a human reading one of these files months later can reconstruct
exactly what happened and why.

The writer here is intentionally small. The CLI layer assembles the
inputs and calls ``write_output_file``; this module just shapes the
JSON and writes it to disk.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Iterable

# Pinned characteristics of the update run that go into every output
# file. If these change, bump the constants here so existing files
# remain interpretable as a snapshot of the prior shape.
_TIERS_LOADED = ["soul", "identity"]
_THINKING_CONFIG = {"effort": "high", "display": "summarized"}


def _ensure_utc(name: str, dt: datetime) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(
            f"{name} must be a UTC-aware datetime (got naive: {dt!r}). "
            "Use datetime.now(timezone.utc), not datetime.utcnow()."
        )


def _filename_safe_iso(dt: datetime) -> str:
    """Render an ISO-8601 UTC timestamp safe for filenames.

    Replaces ``:`` with ``-`` so the file works on filesystems that
    treat colons specially (mac legacy, Windows). Uses ``Z`` for the
    UTC suffix instead of ``+00:00`` for the same reason.
    """
    utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return utc.strftime("%Y-%m-%dT%H-%M-%SZ")


def write_output_file(
    *,
    log_dir: Path,
    group_id: str,
    started_at: datetime,
    completed_at: datetime,
    cache_requested: bool,
    updates: Iterable[dict],
) -> Path:
    """Write the canonical update-run JSON file.

    Returns the path written. Creates ``{log_dir}/outputs/`` if needed.
    """
    _ensure_utc("started_at", started_at)
    _ensure_utc("completed_at", completed_at)

    out_dir = Path(log_dir) / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = f"output-{_filename_safe_iso(started_at)}.json"
    path = out_dir / filename

    duration_ms = int((completed_at - started_at).total_seconds() * 1000)

    payload = {
        "pratyabhijna_version": _pkg_version("pratyabhijna"),
        "group_id": group_id,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": duration_ms,
        "cache": {"requested": cache_requested},
        "bootstrap": {"tiers_loaded": list(_TIERS_LOADED)},
        "thinking_config": dict(_THINKING_CONFIG),
        "updates": list(updates),
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
