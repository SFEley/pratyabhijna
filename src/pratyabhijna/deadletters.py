"""Dead-letter maintenance for the work queue.

Operators use these helpers (via ``python -m pratyabhijna deadletters ...``)
to inspect, retry, or purge tasks that have exhausted their retry budget.

This module talks to the queue's SQLite file directly via ``sqlite3``
rather than going through ``WorkQueue``. The running server sets
journal_mode=WAL on startup, so a second process can safely read and
make brief writes concurrently. All mutations here are single-row or
single-statement updates; ``busy_timeout`` handles any transient lock
contention.

The CLI is usable even when the server is down, which is the scenario
where cleaning dead letters is usually most urgent.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass
class DeadLetter:
    """Summary of a dead-lettered task for display."""

    id: str
    task_type: str
    attempts: int
    max_attempts: int
    updated_at: str
    error_summary: str


@dataclass
class DeadLetterDetail:
    """Full detail of a dead-lettered task."""

    id: str
    task_type: str
    status: str
    attempts: int
    max_attempts: int
    created_at: str
    updated_at: str
    payload: dict[str, Any]
    error: str


def _connect(db_path: str) -> sqlite3.Connection:
    """Open the queue DB with a row factory and busy timeout."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _first_line(text: str | None) -> str:
    """First non-empty line of an error blob, truncated for display."""
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def list_all(db_path: str) -> list[DeadLetter]:
    """Return all dead-lettered tasks, oldest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, task_type, attempts, max_attempts, updated_at, error
               FROM tasks
               WHERE status = 'dead_letter'
               ORDER BY updated_at""",
        ).fetchall()

    return [
        DeadLetter(
            id=row["id"],
            task_type=row["task_type"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            updated_at=row["updated_at"],
            error_summary=_first_line(row["error"]),
        )
        for row in rows
    ]


def resolve_id(db_path: str, prefix: str) -> str:
    """Expand a task-id prefix to a full id.

    Raises ValueError if no dead-lettered task matches, or if the
    prefix is ambiguous (matches more than one).
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id FROM tasks
               WHERE status = 'dead_letter' AND id LIKE ?""",
            (prefix + "%",),
        ).fetchall()

    if not rows:
        raise ValueError(f"No dead-lettered task matches id prefix '{prefix}'")
    if len(rows) > 1:
        matches = ", ".join(row["id"][:8] for row in rows)
        raise ValueError(
            f"Ambiguous prefix '{prefix}' matches {len(rows)} tasks: {matches}"
        )
    return rows[0]["id"]


def show(db_path: str, task_id: str) -> DeadLetterDetail:
    """Return full detail for one dead-lettered task.

    Accepts a full id or a unique prefix. Raises ValueError if the
    id doesn't resolve to a dead-lettered task.
    """
    full_id = resolve_id(db_path, task_id)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (full_id,),
        ).fetchone()

    return DeadLetterDetail(
        id=row["id"],
        task_type=row["task_type"],
        status=row["status"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        payload=json.loads(row["payload"]),
        error=row["error"] or "",
    )


def retry(db_path: str, task_id: str | None = None, all_: bool = False) -> list[str]:
    """Reset dead-lettered tasks to pending so the worker re-runs them.

    Clears attempts, error, and run_at so the task starts fresh.
    Exactly one of ``task_id`` or ``all_=True`` must be provided.
    Returns the list of ids that were reset.
    """
    if all_ and task_id is not None:
        raise ValueError("Pass either task_id or all_, not both")
    if not all_ and task_id is None:
        raise ValueError("Must pass task_id or all_=True")

    with _connect(db_path) as conn:
        if all_:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE status = 'dead_letter'"
                ).fetchall()
            ]
        else:
            ids = [resolve_id(db_path, task_id)]

        if not ids:
            return []

        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"""UPDATE tasks
                SET status = 'pending',
                    attempts = 0,
                    error = NULL,
                    run_at = NULL,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id IN ({placeholders}) AND status = 'dead_letter'""",
            ids,
        )
        conn.commit()

    return ids


def purge(db_path: str, task_id: str | None = None, all_: bool = False) -> list[str]:
    """Delete dead-lettered tasks from the queue permanently.

    Exactly one of ``task_id`` or ``all_=True`` must be provided.
    Returns the list of ids that were deleted.
    """
    if all_ and task_id is not None:
        raise ValueError("Pass either task_id or all_, not both")
    if not all_ and task_id is None:
        raise ValueError("Must pass task_id or all_=True")

    with _connect(db_path) as conn:
        if all_:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE status = 'dead_letter'"
                ).fetchall()
            ]
        else:
            ids = [resolve_id(db_path, task_id)]

        if not ids:
            return []

        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"DELETE FROM tasks WHERE id IN ({placeholders}) AND status = 'dead_letter'",
            ids,
        )
        conn.commit()

    return ids
