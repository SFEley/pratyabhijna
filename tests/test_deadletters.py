"""Tests for the deadletters CLI helpers.

The module operates on the queue's SQLite file directly (sync sqlite3)
so operators can inspect and manage dead-lettered tasks without
running through the MCP server. These tests set up realistic dead
letters via WorkQueue — the same path production uses — then exercise
list/show/retry/purge against the resulting database.
"""

from __future__ import annotations

import pytest

from helpers import wait_for
from pratyabhijna import deadletters as dl
from pratyabhijna.queue import WorkQueue


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

async def _always_fail(payload: dict) -> None:
    raise RuntimeError(f"boom: {payload.get('marker', '?')}")


@pytest.fixture
async def queue_with_dead_letters(tmp_path):
    """A stopped WorkQueue whose DB contains two dead-lettered tasks."""
    db_path = str(tmp_path / "dl.sqlite")
    q = WorkQueue(
        db_path=db_path,
        max_retries=2,
        poll_interval=0.02,
        backoff_base_seconds=0.005,
    )
    q.register("test_task", _always_fail)
    await q.start()

    id_a = await q.enqueue("test_task", {"marker": "alpha"})
    id_b = await q.enqueue("test_task", {"marker": "beta"})

    async def both_dead():
        a = await q.get_task(id_a)
        b = await q.get_task(id_b)
        return a and b and a["status"] == "dead_letter" and b["status"] == "dead_letter"

    await wait_for(both_dead)
    await q.stop()

    yield db_path, id_a, id_b


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------

class TestListAll:
    async def test_returns_dead_letters(self, queue_with_dead_letters):
        db_path, id_a, id_b = queue_with_dead_letters
        rows = dl.list_all(db_path)
        assert len(rows) == 2
        ids = {r.id for r in rows}
        assert ids == {id_a, id_b}

    async def test_summary_fields(self, queue_with_dead_letters):
        db_path, id_a, _ = queue_with_dead_letters
        rows = dl.list_all(db_path)
        row = next(r for r in rows if r.id == id_a)
        assert row.task_type == "test_task"
        assert row.attempts == row.max_attempts == 2
        assert "boom" in row.error_summary
        assert "alpha" in row.error_summary

    async def test_empty_db(self, tmp_path):
        db_path = str(tmp_path / "empty.sqlite")
        q = WorkQueue(db_path=db_path, backoff_base_seconds=0.005)
        q.register("noop", _always_fail)
        await q.start()
        await q.stop()
        assert dl.list_all(db_path) == []


# ---------------------------------------------------------------------------
# resolve_id
# ---------------------------------------------------------------------------

class TestResolveId:
    async def test_full_id(self, queue_with_dead_letters):
        db_path, id_a, _ = queue_with_dead_letters
        assert dl.resolve_id(db_path, id_a) == id_a

    async def test_unique_prefix(self, queue_with_dead_letters):
        db_path, id_a, _ = queue_with_dead_letters
        # First 8 chars of a UUID are almost certainly unique in a 2-row DB
        assert dl.resolve_id(db_path, id_a[:8]) == id_a

    async def test_missing_raises(self, queue_with_dead_letters):
        db_path, _, _ = queue_with_dead_letters
        with pytest.raises(ValueError, match="No dead-lettered task"):
            dl.resolve_id(db_path, "deadbeef")

    async def test_ambiguous_raises(self, tmp_path):
        """A prefix of '' matches everything — treated as ambiguous."""
        db_path = str(tmp_path / "amb.sqlite")
        q = WorkQueue(
            db_path=db_path,
            max_retries=1,
            poll_interval=0.02,
            backoff_base_seconds=0.005,
        )
        q.register("t", _always_fail)
        await q.start()
        await q.enqueue("t", {})
        await q.enqueue("t", {})

        async def both_dead():
            rows = dl.list_all(db_path)
            return len(rows) == 2

        await wait_for(both_dead)
        await q.stop()

        with pytest.raises(ValueError, match="Ambiguous"):
            dl.resolve_id(db_path, "")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

class TestShow:
    async def test_returns_full_detail(self, queue_with_dead_letters):
        db_path, id_a, _ = queue_with_dead_letters
        d = dl.show(db_path, id_a)
        assert d.id == id_a
        assert d.task_type == "test_task"
        assert d.status == "dead_letter"
        assert d.attempts == 2
        assert d.max_attempts == 2
        assert d.payload == {"marker": "alpha"}
        assert "boom: alpha" in d.error
        assert "Traceback" in d.error  # full traceback included

    async def test_accepts_prefix(self, queue_with_dead_letters):
        db_path, id_a, _ = queue_with_dead_letters
        d = dl.show(db_path, id_a[:8])
        assert d.id == id_a


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------

class TestRetry:
    async def test_single_id_resets_to_pending(self, queue_with_dead_letters):
        db_path, id_a, id_b = queue_with_dead_letters
        reset = dl.retry(db_path, task_id=id_a)
        assert reset == [id_a]

        # id_a back to pending, attempts cleared
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (id_a,)).fetchone()
            assert row["status"] == "pending"
            assert row["attempts"] == 0
            assert row["error"] is None
            assert row["run_at"] is None

            # id_b untouched
            row_b = conn.execute("SELECT * FROM tasks WHERE id = ?", (id_b,)).fetchone()
            assert row_b["status"] == "dead_letter"

    async def test_all_resets_every_dead_letter(self, queue_with_dead_letters):
        db_path, id_a, id_b = queue_with_dead_letters
        reset = dl.retry(db_path, all_=True)
        assert set(reset) == {id_a, id_b}
        assert dl.list_all(db_path) == []

    async def test_accepts_prefix(self, queue_with_dead_letters):
        db_path, id_a, _ = queue_with_dead_letters
        reset = dl.retry(db_path, task_id=id_a[:8])
        assert reset == [id_a]

    async def test_all_on_empty_is_noop(self, tmp_path):
        db_path = str(tmp_path / "noop.sqlite")
        q = WorkQueue(db_path=db_path, backoff_base_seconds=0.005)
        q.register("t", _always_fail)
        await q.start()
        await q.stop()
        assert dl.retry(db_path, all_=True) == []

    async def test_rejects_both_id_and_all(self, queue_with_dead_letters):
        db_path, id_a, _ = queue_with_dead_letters
        with pytest.raises(ValueError, match="not both"):
            dl.retry(db_path, task_id=id_a, all_=True)

    async def test_rejects_neither(self, queue_with_dead_letters):
        db_path, _, _ = queue_with_dead_letters
        with pytest.raises(ValueError, match="Must pass"):
            dl.retry(db_path)


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------

class TestPurge:
    async def test_single_id_deletes_row(self, queue_with_dead_letters):
        db_path, id_a, id_b = queue_with_dead_letters
        deleted = dl.purge(db_path, task_id=id_a)
        assert deleted == [id_a]

        import sqlite3
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT id FROM tasks WHERE id = ?", (id_a,)).fetchone()
            assert row is None

            # id_b remains
            row_b = conn.execute("SELECT id FROM tasks WHERE id = ?", (id_b,)).fetchone()
            assert row_b is not None

    async def test_all_deletes_every_dead_letter(self, queue_with_dead_letters):
        db_path, _, _ = queue_with_dead_letters
        deleted = dl.purge(db_path, all_=True)
        assert len(deleted) == 2
        assert dl.list_all(db_path) == []

    async def test_only_deletes_dead_letters(self, tmp_path):
        """purge --all must not touch pending, running, or completed rows."""
        db_path = str(tmp_path / "mixed.sqlite")

        calls = {"fail": 0}

        async def fail_once(payload):
            calls["fail"] += 1
            if payload.get("marker") == "survivor":
                return  # succeeds
            raise RuntimeError("dies")

        q = WorkQueue(
            db_path=db_path,
            max_retries=1,
            poll_interval=0.02,
            backoff_base_seconds=0.005,
        )
        q.register("t", fail_once)
        await q.start()

        dead_id = await q.enqueue("t", {"marker": "victim"})
        ok_id = await q.enqueue("t", {"marker": "survivor"})

        async def both_done():
            a = await q.get_task(dead_id)
            b = await q.get_task(ok_id)
            return (
                a and b
                and a["status"] == "dead_letter"
                and b["status"] == "completed"
            )

        await wait_for(both_done)
        await q.stop()

        dl.purge(db_path, all_=True)

        import sqlite3
        with sqlite3.connect(db_path) as conn:
            remaining = conn.execute("SELECT id, status FROM tasks").fetchall()
        assert len(remaining) == 1
        assert remaining[0][0] == ok_id
        assert remaining[0][1] == "completed"


# ---------------------------------------------------------------------------
# WAL mode (regression)
# ---------------------------------------------------------------------------

class TestWalMode:
    async def test_queue_db_uses_wal(self, tmp_path):
        """WorkQueue.start() must put the DB into WAL mode so the
        deadletters CLI can safely operate concurrently with the server."""
        db_path = str(tmp_path / "wal_check.sqlite")
        q = WorkQueue(db_path=db_path, backoff_base_seconds=0.005)
        q.register("t", _always_fail)
        await q.start()
        try:
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            await q.stop()
