"""Persistent async work queue backed by SQLite.

All slow operations (Graphiti episode processing, identity synthesis rebuilds)
go through this queue. MCP tools enqueue tasks and return immediately;
a single background worker processes them sequentially.

Graphiti requires episodes be added one at a time, each fully awaited
before the next. The worker loop enforces this.
"""

import asyncio
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import aiosqlite

TaskHandler = Callable[[dict[str, Any]], Awaitable[None]]

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    task_type    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkQueue:
    """Persistent task queue for async memory operations."""

    def __init__(self, db_path: str, max_retries: int = 3, poll_interval: float = 0.5):
        self.db_path = db_path
        self.max_retries = max_retries
        self.poll_interval = poll_interval
        self._handlers: dict[str, TaskHandler] = {}
        self._db: aiosqlite.Connection | None = None
        self._worker_task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    # --- Lifecycle ---

    async def start(self) -> None:
        """Open DB, create schema, recover crashed tasks, start worker loop."""
        if self._running:
            return

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._recover_crashed()
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self, timeout: float = 10.0) -> None:
        """Signal worker to stop, wait for current task, close DB."""
        if not self._running:
            return

        self._running = False
        if self._worker_task:
            try:
                await asyncio.wait_for(self._worker_task, timeout=timeout)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
            self._worker_task = None

        if self._db:
            await self._db.close()
            self._db = None

    # --- Handler registration ---

    def register(self, task_type: str, handler: TaskHandler) -> None:
        """Register a handler for a task type. Call before start()."""
        self._handlers[task_type] = handler

    # --- Enqueueing ---

    async def enqueue(self, task_type: str, payload: dict[str, Any]) -> str:
        """Insert a pending task. Returns task ID (UUID).

        Raises ValueError if task_type has no registered handler.
        """
        if task_type not in self._handlers:
            raise ValueError(f"Cannot enqueue '{task_type}': no handler registered")

        task_id = str(uuid4())
        now = _now()
        await self._db.execute(
            """INSERT INTO tasks (id, task_type, payload, status, attempts,
                                  max_attempts, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (task_id, task_type, json.dumps(payload), self.max_retries, now, now),
        )
        await self._db.commit()
        return task_id

    # --- Status queries ---

    async def depth(self) -> int:
        """Count of pending + running tasks."""
        async with self._db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'running')"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]

    async def last_write(self) -> str | None:
        """ISO timestamp of most recent completed task, or None."""
        async with self._db.execute(
            "SELECT completed_at FROM tasks WHERE status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_task(self, task_id: str) -> dict | None:
        """Return full task row as dict, or None if not found."""
        async with self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def dead_letters(self) -> list[dict]:
        """Return all dead-lettered tasks."""
        async with self._db.execute(
            "SELECT * FROM tasks WHERE status = 'dead_letter' ORDER BY updated_at"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # --- Internal ---

    async def _recover_crashed(self) -> None:
        """Reset any 'running' tasks back to 'pending' on startup."""
        await self._db.execute(
            "UPDATE tasks SET status = 'pending', updated_at = ? WHERE status = 'running'",
            (_now(),),
        )
        await self._db.commit()

    async def _worker_loop(self) -> None:
        """Process one task at a time, polling for pending tasks."""
        while self._running:
            task = await self._claim_next()
            if task is None:
                await asyncio.sleep(self.poll_interval)
                continue
            await self._process(task)

    async def _claim_next(self) -> dict | None:
        """Atomically claim the oldest pending task."""
        async with self._db.execute(
            "SELECT * FROM tasks WHERE status = 'pending' ORDER BY created_at LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            task = dict(row)

        await self._db.execute(
            "UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ? AND status = 'pending'",
            (_now(), task["id"]),
        )
        await self._db.commit()
        return task

    async def _process(self, task: dict) -> None:
        """Run handler, mark completed or handle failure."""
        handler = self._handlers[task["task_type"]]
        try:
            payload = json.loads(task["payload"])
            await handler(payload)
            await self._mark_completed(task["id"])
        except Exception as e:
            attempts = task["attempts"] + 1
            await self._handle_failure(task["id"], attempts, task["max_attempts"], e)

    async def _mark_completed(self, task_id: str) -> None:
        now = _now()
        await self._db.execute(
            "UPDATE tasks SET status = 'completed', updated_at = ?, completed_at = ? WHERE id = ?",
            (now, now, task_id),
        )
        await self._db.commit()

    async def _handle_failure(
        self, task_id: str, attempts: int, max_attempts: int, error: Exception
    ) -> None:
        error_text = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
        now = _now()

        if attempts >= max_attempts:
            await self._db.execute(
                "UPDATE tasks SET status = 'dead_letter', attempts = ?, "
                "error = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (attempts, error_text, now, now, task_id),
            )
        else:
            await self._db.execute(
                "UPDATE tasks SET status = 'pending', attempts = ?, "
                "error = ?, updated_at = ? WHERE id = ?",
                (attempts, error_text, now, task_id),
            )
        await self._db.commit()
