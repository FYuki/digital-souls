"""バックグラウンドtaskの起動・停止ライフサイクルを管理する共通部品。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class ManagedTask:
    """単一のバックグラウンドtaskの二重起動防止と停止待機を提供する。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    def spawn(
        self, coroutine: Coroutine[Any, Any, None], *, restart_done: bool = False
    ) -> asyncio.Task[None]:
        """起動中なら二重起動を拒否し、coroutineをtaskとして起動する。"""
        if self._task is not None and (not restart_done or not self._task.done()):
            raise RuntimeError(f"{self._name} is already running")
        self._task = asyncio.create_task(coroutine)
        return self._task

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def stop(self) -> None:
        """taskの終了を待機して参照を外す。"""
        task = self._task
        if task is None:
            return
        try:
            await task
        finally:
            self._task = None
