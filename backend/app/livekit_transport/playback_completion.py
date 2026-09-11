"""応答全体の再生確認を、生成完了後・Core terminal化前に待つ。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class PlaybackCompletionGate:
    def __init__(self, timeout_seconds: float = 10) -> None:
        if timeout_seconds <= 0:
            raise ValueError("playback completion timeout must be positive")
        self._timeout = timeout_seconds
        self._pending: dict[str, tuple[int, asyncio.Future[None]]] = {}

    async def wait(self, response_id: str, last_audio_sequence: int,
                   prepare: Callable[[], Awaitable[None]]) -> None:
        if last_audio_sequence < 1 or response_id in self._pending:
            raise ValueError("invalid or duplicate response completion wait")
        ready = asyncio.get_running_loop().create_future()
        self._pending[response_id] = (last_audio_sequence, ready)
        try:
            # 総sample数の送信中に届いた確認も失わないよう、送信前に登録する。
            await prepare()
            await asyncio.wait_for(ready, self._timeout)
        finally:
            self._pending.pop(response_id, None)
            if not ready.done():
                ready.cancel()

    def confirm(self, response_id: str, last_audio_sequence: int) -> bool:
        pending = self._pending.get(response_id)
        if pending is None or pending[0] != last_audio_sequence or pending[1].done():
            return False
        pending[1].set_result(None)
        return True
