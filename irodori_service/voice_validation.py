from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from irodori_service.contracts import ServiceError

T = TypeVar("T")


class VoiceValidationPool:
    """音声検証の実行数を制限し、推論・再準備用executorから分離する。"""

    def __init__(self, limit: int) -> None:
        self._slots = asyncio.Semaphore(limit)
        self._executor = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="irodori-voice-check")

    async def run(self, operation: Callable[[], T]) -> T:
        # acquireが即時成功する場合はawaitで制御を渡さず、待機queueを作らない。
        if self._slots.locked():
            raise ServiceError("tts_capacity_exceeded", 429)
        await self._slots.acquire()
        try:
            pending = asyncio.get_running_loop().run_in_executor(self._executor, operation)
        except BaseException:
            self._slots.release()
            raise

        def finished(result: asyncio.Future[T]) -> None:
            self._slots.release()
            # HTTP取消後の検証失敗も、未回収例外として本文・pathをlogへ流さない。
            if not result.cancelled():
                result.exception()

        pending.add_done_callback(finished)
        # HTTP取消だけでは実際のファイル読込を止められない。終了するまで枠を保持する。
        return await asyncio.shield(pending)

    async def close(self) -> None:
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
