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
    def generate(self, transcript: str) -> AsyncIterator[TextDelta]:
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
    async def stop_response(self, response: Response) -> ResponseStopResult:
        """送出停止と、その応答の最終出力停止を確認する。欠測は例外にする。

        呼出元の取消を受けたら待機資源を解放する。確認前のtimeoutや切断を
        成功として返してはいけない。応答・世代・要求ごとの相関は実装側で検証する。
        """
        ...
