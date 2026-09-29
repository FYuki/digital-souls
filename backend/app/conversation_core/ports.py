from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.conversation_core.models import (
    AudioSegment,
    CoreEvent,
    Response,
    StageObservation,
    ResponseStartResult,
    ResponseStopResult,
    TerminalOutcome,
    TextDelta,
)


class SttPort(Protocol):
    async def transcribe(self, audio: bytes) -> str:
        ...


class LlmPort(Protocol):
    def generate(self, transcript: str, *, response: Response) -> AsyncIterator[TextDelta]:
        ...


class TtsPort(Protocol):
    def synthesize(self, text: str) -> AsyncIterator[AudioSegment]:
        ...


class DeliveryPort(Protocol):
    async def publish(self, event: CoreEvent) -> None:
        ...


class PersistencePort(Protocol):
    async def start_response(
        self, *, response_id: str, user_content: str
    ) -> ResponseStartResult:
        ...

    async def persist(self, outcome: TerminalOutcome) -> None:
        ...


class ObservationPort(Protocol):
    async def record(self, observation: StageObservation) -> None:
        ...


class ResponseCompletionPort(Protocol):
    async def finish_response(self, response: Response) -> None:
        """出力完了まで待つ。待機中の応答はCoreがcancelできる。"""
        ...


class ResponseCancellationPort(Protocol):
    async def stop_response(
        self, response: Response, *, decided_at_ns: int
    ) -> ResponseStopResult:
        """送出を停止し、decided_at_ns 時点の再生済みprefixを返す。

        decided_at_ns は中断判断の確定時刻（take-turn確定または取消受理）を
        単調時計で表したもの。FE等の外部確認を待ってはいけない。
        呼出元の取消を受けたら待機資源を解放する。停止失敗や欠測は例外にする。
        """
        ...
