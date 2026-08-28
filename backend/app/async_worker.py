from __future__ import annotations

from collections.abc import Callable
from functools import partial
import threading
from typing import TypeVar

import anyio


Result = TypeVar("Result")
_CAPACITY = threading.BoundedSemaphore(8)


class SyncWorkerCapacityError(RuntimeError):
    """同期workerの固定上限を超えた。"""


async def run_sync(
    operation: Callable[..., Result], *args: object, **kwargs: object
) -> Result:
    """AnyIO管理workerで同期処理を固定上限つきで実行する。"""
    if not _CAPACITY.acquire(blocking=False):
        raise SyncWorkerCapacityError("synchronous worker capacity exceeded")

    def invoke() -> Result:
        try:
            return partial(operation, *args, **kwargs)()
        finally:
            _CAPACITY.release()

    return await anyio.to_thread.run_sync(invoke, abandon_on_cancel=True)
