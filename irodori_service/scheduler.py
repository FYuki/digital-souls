from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Literal, Protocol

from irodori_service.config import ServiceConfig
from irodori_service.contracts import ServiceError, SpeechRequest, validate_wav

Environment = Literal["dev", "test", "dogfood"]


class SynthesisWorker(Protocol):
    @property
    def ready(self) -> bool: ...
    def start(self) -> None: ...
    def synthesize(self, payload: SpeechRequest) -> bytes: ...
    def close(self) -> None: ...


@dataclass
class PendingRequest:
    payload: SpeechRequest
    result: asyncio.Future[bytes]
    started: asyncio.Event


class SynthesisScheduler:
    """単一workerの所有を保ち、待機中だけdogfoodを優先する。"""

    def __init__(self, worker: SynthesisWorker, config: ServiceConfig) -> None:
        self.worker = worker
        self.config = config
        self._dogfood: deque[PendingRequest] = deque()
        self._development: deque[PendingRequest] = deque()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._active: PendingRequest | None = None

    @property
    def ready(self) -> bool:
        return not self._closed and self.worker.ready

    @property
    def pending_count(self) -> int:
        return len(self._dogfood) + len(self._development)

    @property
    def active_count(self) -> int:
        return int(self._active is not None)

    async def start(self) -> None:
        await asyncio.to_thread(self.worker.start)
        self._task = asyncio.create_task(self._run())

    async def submit(self, payload: SpeechRequest, environment: Environment) -> bytes:
        if not self.ready:
            raise ServiceError("tts_not_ready", 503)
        if self.pending_count >= self.config.max_pending:
            raise ServiceError("tts_capacity_exceeded", 429)
        item = PendingRequest(payload, asyncio.get_running_loop().create_future(), asyncio.Event())
        queue = self._dogfood if environment == "dogfood" else self._development
        queue.append(item)
        self._wake.set()
        try:
            try:
                await asyncio.wait_for(item.started.wait(), self.config.queue_timeout)
            except asyncio.TimeoutError as error:
                # dispatchとtimeoutが競合した場合は、実行済み要求へ待機timeoutを投影しない。
                if not item.started.is_set():
                    raise ServiceError("tts_queue_timeout", 504) from error
            return await asyncio.shield(item.result)
        finally:
            if not item.result.done():
                item.result.cancel()
            if item in queue:
                queue.remove(item)

    async def _run(self) -> None:
        while not self._closed:
            if not self.worker.ready:
                self._fail_pending()
                try:
                    await asyncio.to_thread(self.worker.start)
                except Exception:  # noqa: BLE001 - 外部runtimeの詳細を公開せず安定した失敗へ変換する
                    # 自動再ロードの無限loopは作らず、サービス再起動で復旧する。
                    self._closed = True
                    self._fail_pending()
                    return
            if not self.pending_count:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
                continue
            queue = self._dogfood if self._dogfood else self._development
            item = queue.popleft()
            if item.result.cancelled():
                continue
            self._active = item
            item.started.set()
            try:
                # await元が切断しても実処理が終わるまでworker枠を解放しない。
                audio = await asyncio.to_thread(self.worker.synthesize, item.payload)
                validate_wav(audio)
                if not item.result.done():
                    item.result.set_result(audio)
            except ServiceError as error:
                if not item.result.done():
                    item.result.set_exception(error)
            except Exception:  # noqa: BLE001 - 外部runtimeの詳細を公開せず安定した失敗へ変換する
                if not item.result.done():
                    item.result.set_exception(ServiceError("tts_worker_failed"))
            finally:
                self._active = None

    def _fail_pending(self) -> None:
        for queue in (self._dogfood, self._development):
            while queue:
                item = queue.popleft()
                item.started.set()
                if not item.result.done():
                    item.result.set_exception(ServiceError("tts_not_ready", 503))

    async def close(self) -> None:
        self._closed = True
        self._fail_pending()
        self._wake.set()
        await asyncio.to_thread(self.worker.close)
        if self._task is not None:
            await self._task
