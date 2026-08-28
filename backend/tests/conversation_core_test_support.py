from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from io import BytesIO
from typing import Iterator

from app.conversation_core.models import AudioSegment, ResponseStartResult, TextDelta


def make_pcm16_wav(
    *,
    pcm: bytes,
    sample_rate: int,
    channels: int,
) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


@dataclass
class RecordingDelivery:
    events: list[object] = field(default_factory=list)

    async def publish(self, event: object) -> None:
        self.events.append(event)


@dataclass
class BlockingDelivery:
    blocked_type: str
    events: list[object] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def publish(self, event: object) -> None:
        if getattr(event, "type") == self.blocked_type:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        self.events.append(event)


@dataclass
class RecordingPersistence:
    starts: list[tuple[str, str]] = field(default_factory=list)
    outcomes: list[object] = field(default_factory=list)
    content_skipped: bool = False

    async def start_response(
        self, *, response_id: str, user_content: str
    ) -> ResponseStartResult:
        self.starts.append((response_id, user_content))
        return ResponseStartResult(content_skipped=self.content_skipped)

    async def persist(self, outcome: object) -> None:
        self.outcomes.append(outcome)


@dataclass
class BlockingTerminalPersistence(RecordingPersistence):
    persist_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_persist: asyncio.Event = field(default_factory=asyncio.Event)

    async def persist(self, outcome: object) -> None:
        self.persist_started.set()
        await self.release_persist.wait()
        await super().persist(outcome)


@dataclass
class RecordingObservation:
    observations: list[object] = field(default_factory=list)

    async def record(self, observation: object) -> None:
        self.observations.append(observation)


@dataclass
class RecordingStt:
    transcript: str = "書き起こし"
    calls: list[bytes] = field(default_factory=list)

    async def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        return self.transcript


@dataclass
class FailingStt:
    error: Exception
    calls: list[bytes] = field(default_factory=list)

    async def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        raise self.error


@dataclass
class BlockingLlm:
    calls: list[str] = field(default_factory=list)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def generate(self, transcript: str) -> AsyncIterator[TextDelta]:
        self.calls.append(transcript)
        await self.release.wait()
        if False:
            yield TextDelta(1, "", (0, 0))


@dataclass
class RecordingLlm:
    deltas: tuple[TextDelta, ...]
    calls: list[str] = field(default_factory=list)

    async def generate(self, transcript: str) -> AsyncIterator[TextDelta]:
        self.calls.append(transcript)
        for delta in self.deltas:
            yield delta


@dataclass
class RecordingTts:
    calls: list[str] = field(default_factory=list)

    async def synthesize(self, text: str) -> AsyncIterator[AudioSegment]:
        self.calls.append(text)
        yield AudioSegment(1, b"audio", (0, len(text)))


def response_id_factory(*response_ids: str):
    identifiers: Iterator[str] = iter(response_ids)
    return lambda: next(identifiers)


def event_field(value: object, field_name: str) -> object:
    if isinstance(value, dict):
        return value[field_name]
    return getattr(value, field_name)


def event_fields(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    return vars(value)
