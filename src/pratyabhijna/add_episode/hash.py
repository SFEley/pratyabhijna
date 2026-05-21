"""Episode-body hash for Stage 0 idempotency.

Hash inputs include everything that defines episode identity at the
write-path level: group_id, source type, source_description, reference_time,
and the body itself. Two calls with identical inputs collapse — closing the
door on queue retries and double-enqueues silently re-processing the same
content. Differing inputs (same body recorded at different times, same
body from different sources) produce distinct hashes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from graphiti_core.nodes import EpisodeType


def episode_hash(
    *,
    group_id: str,
    source: EpisodeType,
    source_description: str,
    reference_time: datetime,
    body: str,
) -> str:
    """Return a stable sha256 hex digest of the inputs that define episode identity."""
    parts = [
        group_id,
        source.value,
        source_description,
        reference_time.isoformat(),
        body,
    ]
    payload = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
