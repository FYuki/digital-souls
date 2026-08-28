from __future__ import annotations

import asyncio
import threading

from app.async_worker import run_sync


def test_cancellation_does_not_wait_for_abandoned_sync_operation() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_operation() -> None:
        entered.set()
        release.wait(timeout=2)

    async def exercise() -> None:
        task = asyncio.create_task(run_sync(blocking_operation))
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=0.2)
        except asyncio.CancelledError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError("cancelled sync operation must raise CancelledError")
        finally:
            release.set()

    asyncio.run(exercise())
