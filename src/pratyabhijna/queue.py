"""Persistent async work queue backed by SQLite.

All slow operations (Graphiti episode processing, identity synthesis rebuilds)
go through this queue. MCP tools enqueue tasks and return immediately;
a single background worker processes them sequentially.

Graphiti requires episodes be added one at a time, each fully awaited
before the next. The worker loop enforces this.

Tasks carry an optional ``run_at`` timestamp. The worker skips any task
whose ``run_at`` is still in the future, which supports two use cases:

1. Deferred/singleton scheduling for synthesis rebuilds (write handlers
   call ``reschedule_or_enqueue`` to kick a future task forward).
2. Exponential backoff on retries — ``_handle_failure`` pushes ``run_at``
   forward by ``attempts² × backoff_base_seconds`` so transient upstream
   failures (rate limits, brief outages) have time to resolve.
"""

import asyncio
import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import aiosqlite

from pratyabhijna.log import get_logger

_log = get_logger(__name__)

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
    completed_at TEXT,
    run_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_run_at ON tasks(run_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt: datetime) -> str:
    """Serialize a datetime as ISO-8601 UTC. Naive datetimes assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class WorkQueue:
    """Persistent task queue for async memory operations."""

    def __init__(
        self,
        db_path: str,
        max_retries: int = 10,
        poll_interval: float = 0.5,
        backoff_base_seconds: float = 60.0,
    ):
        self.db_path = db_path
        self.max_retries = max_retries
        self.poll_interval = poll_interval
        self.backoff_base_seconds = backoff_base_seconds
        self._handlers: dict[str, TaskHandler] = {}
        self._db: aiosqlite.Connection | None = None
        self._worker_task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    # --- Lifecycle ---

    async def start(self, run_worker: bool = True) -> None:
        """Open DB, create schema, recover crashed tasks, start worker loop.

        Pass run_worker=False to open the DB for enqueuing only, without
        starting the worker loop. Useful for CLI commands that write tasks
        intended for the server process to execute.
        """
        if self._running:
            return

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        # WAL lets a separate reader/writer (the deadletters CLI) operate
        # on the queue file concurrently with the server. busy_timeout
        # gives brief contention a chance to resolve before erroring.
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        self._running = True
        if run_worker:
            await self._recover_crashed()
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def _migrate(self) -> None:
        """Apply in-place schema upgrades for existing databases.

        ``CREATE TABLE IF NOT EXISTS`` does not add columns to a table
        that already exists, so any column added after the first release
        needs an explicit ``ALTER TABLE``. Each migration checks current
        columns via ``PRAGMA table_info`` and is a no-op when already
        applied.
        """
        async with self._db.execute("PRAGMA table_info(tasks)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}

        if "run_at" not in columns:
            await self._db.execute("ALTER TABLE tasks ADD COLUMN run_at TEXT")
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_run_at ON tasks(run_at)"
            )
            await self._db.commit()
            _log.info("migrated queue schema: added run_at column")

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

    async def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        run_at: datetime | None = None,
    ) -> str:
        """Insert a pending task. Returns task ID (UUID).

        If ``run_at`` is given, the worker will not claim the task until
        that time has passed. ``None`` (the default) means run as soon
        as the worker is free.

        Raises ValueError if task_type has no registered handler.
        """
        if task_type not in self._handlers:
            raise ValueError(f"Cannot enqueue '{task_type}': no handler registered")

        task_id = str(uuid4())
        now = _now()
        run_at_iso = _iso(run_at) if run_at is not None else None
        await self._db.execute(
            """INSERT INTO tasks (id, task_type, payload, status, attempts,
                                  max_attempts, created_at, updated_at, run_at)
               VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (
                task_id,
                task_type,
                json.dumps(payload),
                self.max_retries,
                now,
                now,
                run_at_iso,
            ),
        )
        await self._db.commit()
        return task_id

    async def reschedule_or_enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        run_at: datetime,
    ) -> str:
        """Singleton scheduling: ensure exactly one pending task of this type.

        If a pending task of ``task_type`` already exists, update its
        ``run_at`` (and payload) in place and return its id. Otherwise
        insert a new task. Running and completed tasks are ignored —
        a running task does not block creating a new pending one, and
        completed tasks are inert.

        Used by write handlers to schedule identity synthesis rebuilds:
        rapid successive writes kick the same rebuild forward instead
        of piling up redundant rebuild tasks.
        """
        if task_type not in self._handlers:
            raise ValueError(
                f"Cannot reschedule_or_enqueue '{task_type}': no handler registered"
            )

        run_at_iso = _iso(run_at)
        now = _now()

        async with self._db.execute(
            """SELECT id FROM tasks
               WHERE task_type = ? AND status = 'pending'
               ORDER BY created_at
               LIMIT 1""",
            (task_type,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is not None:
            task_id = row[0]
            await self._db.execute(
                """UPDATE tasks
                   SET run_at = ?, payload = ?, updated_at = ?
                   WHERE id = ?""",
                (run_at_iso, json.dumps(payload), now, task_id),
            )
            await self._db.commit()
            return task_id

        return await self.enqueue(task_type, payload, run_at=run_at)

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

    async def last_error(self) -> dict | None:
        """Return task_id, error text, and updated_at of most recent dead-letter, or None."""
        async with self._db.execute(
            "SELECT id, error, updated_at FROM tasks WHERE status = 'dead_letter' "
            "ORDER BY updated_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return {"task_id": row[0], "error": row[1], "updated_at": row[2]}

    # --- Internal ---

    async def _recover_crashed(self) -> None:
        """Recover tasks left in 'running' state by a previous service crash.

        Called only when run_worker=True (i.e. the main service process, not a
        CLI enqueue call). Each interrupted task gets its attempt count
        incremented and is rescheduled with the standard backoff delay. Tasks
        that have exhausted max_attempts are dead-lettered instead.
        """
        async with self._db.execute(
            "SELECT id, task_type, attempts, max_attempts FROM tasks WHERE status = 'running'"
        ) as cursor:
            crashed = await cursor.fetchall()

        if not crashed:
            return

        now_dt = datetime.now(timezone.utc)
        now = _iso(now_dt)
        error_text = "ServiceRestart: task was interrupted when the service stopped"

        for row in crashed:
            task_id, task_type, attempts, max_attempts = row[0], row[1], row[2], row[3]
            attempts += 1

            if attempts >= max_attempts:
                await self._db.execute(
                    "UPDATE tasks SET status = 'dead_letter', attempts = ?, "
                    "error = ?, updated_at = ? WHERE id = ?",
                    (attempts, error_text, now, task_id),
                )
                _log.error(
                    "task %s:%s dead-lettered after service restart "
                    "(attempt %d/%d exhausted)",
                    task_type, task_id, attempts, max_attempts,
                )
            else:
                delay_seconds = (attempts ** 2) * self.backoff_base_seconds
                next_run_at = _iso(now_dt + timedelta(seconds=delay_seconds))
                await self._db.execute(
                    "UPDATE tasks SET status = 'pending', attempts = ?, "
                    "error = ?, updated_at = ?, run_at = ? WHERE id = ?",
                    (attempts, error_text, now, next_run_at, task_id),
                )
                _log.error(
                    "task %s:%s was interrupted by service restart "
                    "(attempt %d/%d), retrying in %.1fs",
                    task_type, task_id, attempts, max_attempts, delay_seconds,
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
        """Atomically claim the oldest eligible pending task.

        A task is eligible when its ``run_at`` is NULL or has already
        passed. Uses a single UPDATE with a subquery so the transition
        from 'pending' to 'running' is one statement. If another worker
        claimed the row first (multi-worker scenario), the subquery
        finds no eligible row and the UPDATE touches zero rows.
        After committing, we fetch the claimed task by status.
        """
        now = _now()
        cursor = await self._db.execute(
            """UPDATE tasks
               SET status = 'running', updated_at = ?
               WHERE id = (
                   SELECT id FROM tasks
                   WHERE status = 'pending'
                     AND (run_at IS NULL OR run_at <= ?)
                   ORDER BY created_at
                   LIMIT 1
               )""",
            (now, now),
        )
        if cursor.rowcount == 0:
            # commit, not rollback: aiosqlite shares one connection, so a
            # concurrent enqueue() INSERT can join this transaction between
            # our UPDATE and the cleanup. rollback() would take that INSERT
            # down with the no-op UPDATE; commit() ends the transaction and
            # releases the RESERVED lock just the same.
            await self._db.commit()
            return None
        await self._db.commit()

        async with self._db.execute(
            "SELECT * FROM tasks WHERE status = 'running' ORDER BY updated_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _process(self, task: dict) -> None:
        """Run handler, mark completed or handle failure."""
        handler = self._handlers[task["task_type"]]
        _log.debug("processing task %s:%s", task["task_type"], task["id"])
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
        _log.debug("task %s completed", task_id)

    async def _handle_failure(
        self, task_id: str, attempts: int, max_attempts: int, error: Exception
    ) -> None:
        error_text = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
        now_dt = datetime.now(timezone.utc)
        now = _iso(now_dt)

        if attempts >= max_attempts:
            await self._db.execute(
                "UPDATE tasks SET status = 'dead_letter', attempts = ?, "
                "error = ?, updated_at = ? WHERE id = ?",
                (attempts, error_text, now, task_id),
            )
            _log.error("task %s dead-lettered after %d attempts: %s", task_id, attempts, error)
        else:
            # Exponential backoff: attempts² × base gives 1, 4, 9, 16, 25…
            # multiples of the base (default 60s). At base=60 and
            # max_retries=10 the final attempt lands ~6.4h after the
            # first failure — enough time for transient upstream issues
            # (rate limits, brief outages) to resolve without burning
            # retries too fast.
            delay_seconds = (attempts ** 2) * self.backoff_base_seconds
            next_run_at = _iso(now_dt + timedelta(seconds=delay_seconds))
            await self._db.execute(
                "UPDATE tasks SET status = 'pending', attempts = ?, "
                "error = ?, updated_at = ?, run_at = ? WHERE id = ?",
                (attempts, error_text, now, next_run_at, task_id),
            )
            _log.warning(
                "task %s failed (attempt %d/%d), retrying in %.1fs: %s",
                task_id,
                attempts,
                max_attempts,
                delay_seconds,
                error,
            )
        await self._db.commit()


# ---------------------------------------------------------------------------
# Module-level stats reader (shared by MCP status and CLI status)
# ---------------------------------------------------------------------------

def collect_queue_stats(db_path: str) -> dict:
    """Read queue counters from SQLite without instantiating a WorkQueue.

    Both the MCP `status` tool and the `python -m pratyabhijna status`
    subcommand use this to surface queue health. Starting a ``WorkQueue``
    from the CLI path would run ``_recover_crashed`` (clobbering any task
    the live server has in-flight) and spawn a second worker loop.

    Uses stdlib ``sqlite3`` for a brief read-only connection; the WAL mode
    set by the running WorkQueue lets this coexist safely.

    Returns a dict with:
      - ``depth`` — count of pending + running tasks
      - ``last_write`` — ISO timestamp of most recent completed task, or None
      - ``dead_letters`` — count of dead-lettered tasks
      - ``last_error`` — `{task_id, error, updated_at}` of the most recent
        dead-letter, or None
      - ``by_task_type`` — nested dict ``{task_type: {status: count}}`` with
        one entry per (task_type, status) pair that has any rows
    """
    import sqlite3

    if not Path(db_path).exists():
        return {
            "depth": 0,
            "last_write": None,
            "dead_letters": 0,
            "last_error": None,
            "by_task_type": {},
        }

    with sqlite3.connect(db_path, timeout=5.0) as conn:
        depth = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'running')"
        ).fetchone()[0]
        last_write_row = conn.execute(
            "SELECT completed_at FROM tasks WHERE status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        dead_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'dead_letter'"
        ).fetchone()[0]
        last_err_row = conn.execute(
            "SELECT id, error, updated_at FROM tasks WHERE status = 'dead_letter' "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        breakdown_rows = conn.execute(
            "SELECT task_type, status, COUNT(*) FROM tasks GROUP BY task_type, status"
        ).fetchall()

    by_task_type: dict[str, dict[str, int]] = {}
    for task_type, status, count in breakdown_rows:
        by_task_type.setdefault(task_type, {})[status] = count

    return {
        "depth": depth,
        "last_write": last_write_row[0] if last_write_row else None,
        "dead_letters": dead_count,
        "last_error": (
            {"task_id": last_err_row[0], "error": last_err_row[1],
             "updated_at": last_err_row[2]}
            if last_err_row
            else None
        ),
        "by_task_type": by_task_type,
    }
