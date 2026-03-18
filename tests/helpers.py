"""Shared test helpers for Vesper memory server tests."""

import asyncio


async def wait_for(condition, timeout=2.0, interval=0.02):
    """Poll ``condition()`` (sync or async) until truthy, or raise on timeout.

    Useful for waiting on background queue processing in tests.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(interval)
    raise TimeoutError("Condition not met within timeout")
