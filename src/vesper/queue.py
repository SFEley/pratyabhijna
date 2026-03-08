"""Work queue abstraction.

Persistent async task queue backed by SQLite.
Full implementation in Phase 6.
"""

# Stub — implementation in Phase 6


class WorkQueue:
    """Persistent task queue for async memory operations."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    @property
    def depth(self) -> int:
        """Number of pending tasks."""
        return 0

    @property
    def last_write(self) -> str | None:
        """ISO timestamp of last completed write, or None."""
        return None
