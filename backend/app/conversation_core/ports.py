from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.conversation_core.models import (
    AudioSegment,
    CoreEvent,
    StageObservation,
    ResponseStartResult,
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
