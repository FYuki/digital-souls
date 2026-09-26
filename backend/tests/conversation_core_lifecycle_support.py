from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import pytest

from app.conversation_core.models import ResponseStartResult
from tests.conversation_core_test_support import (
    BlockingDelivery,
    BlockingLlm,
    BlockingTerminalPersistence,
    FailingStt,
    RecordingDelivery,
    RecordingLlm,
    RecordingObservation,
    RecordingPersistence,
    RecordingStt,
    RecordingTts,
    event_field,
    event_fields,
    response_id_factory,
)


SESSION_ID = "20000000-0000-4000-8000-000000000107"
RESPONSE_1 = "50000000-0000-4000-8000-000000000107"
RESPONSE_2 = "50000000-0000-4000-8000-000000000108"
UTTERANCE_1 = "30000000-0000-4000-8000-000000000107"
UTTERANCE_2 = "30000000-0000-4000-8000-000000000108"
UTTERANCE_3 = "30000000-0000-4000-8000-000000000109"


def _core_module():
    try:
        return importlib.import_module("app.conversation_core")
    except ModuleNotFoundError as error:
        if error.name == "app.conversation_core":
            pytest.fail("app.conversation_core must implement the Conversation Core")
        raise


def _session(*, response_ids: tuple[str, ...] = (RESPONSE_1, RESPONSE_2)):
    module = _core_module()
    delivery = RecordingDelivery()
    persistence = RecordingPersistence()
    observation = RecordingObservation()
    session = module.ConversationCoreSession(
        session_id=SESSION_ID,
        response_id_factory=response_id_factory(*response_ids),
        delivery=delivery,
        persistence=persistence,
        observation=observation,
        stt=RecordingStt(),
        llm=BlockingLlm(),
        tts=RecordingTts(),
    )
    return module, session, delivery, persistence, observation


def _automatic_session(*, stt: object, llm: object, tts: object):
    module = _core_module()
    delivery = RecordingDelivery()
    persistence = RecordingPersistence()
    observation = RecordingObservation()
    session = module.ConversationCoreSession(
        session_id=SESSION_ID,
        response_id_factory=response_id_factory(RESPONSE_1, RESPONSE_2),
        delivery=delivery,
        persistence=persistence,
        observation=observation,
        stt=stt,
        llm=llm,
        tts=tts,
    )
    return module, session, delivery, persistence, observation


def _run(exercise: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(exercise())


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 1.0
) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(poll(), timeout=timeout)
    except TimeoutError as error:
        raise AssertionError("期待した非同期状態へ到達しなかった") from error


def _response_started_events(delivery: RecordingDelivery) -> list[object]:
    return [
        event
        for event in delivery.events
        if event_field(event, "type") == "response_started"
    ]


def _terminal_events(delivery: RecordingDelivery) -> list[object]:
    terminal_types = {
        "response_completed",
        "response_cancelled",
        "response_failed",
        "response_privacy_skipped",
    }
    return [
        event
        for event in delivery.events
        if event_field(event, "type") in terminal_types
    ]


async def _blocked_until_cancelled(
    *,
    started: asyncio.Event,
    cancelled: asyncio.Event,
    blocker: asyncio.Event,
) -> None:
    started.set()
    try:
        await blocker.wait()
    except asyncio.CancelledError:
        cancelled.set()
        raise


@dataclass
class CancellationSuppressingLlm:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancellation_received: asyncio.Event = field(default_factory=asyncio.Event)
    blocker: asyncio.Event = field(default_factory=asyncio.Event)

    async def generate(self, transcript: str, *, response: object | None = None) -> AsyncIterator[object]:
        module = _core_module()
        self.started.set()
        try:
            await self.blocker.wait()
        except asyncio.CancelledError:
            self.cancellation_received.set()
        yield module.TextDelta(1, "遅延結果", (0, 4))


@dataclass
class FirstCallFailingLlm:
    calls: list[str] = field(default_factory=list)
    next_call_blocker: asyncio.Event = field(default_factory=asyncio.Event)

    async def generate(self, transcript: str, *, response: object | None = None) -> AsyncIterator[object]:
        self.calls.append(transcript)
        if len(self.calls) == 1:
            raise RuntimeError("llm failure sentinel")
        await self.next_call_blocker.wait()
        if False:
            yield _core_module().TextDelta(1, "", (0, 0))


@dataclass
class FirstCallFailingTts:
    calls: list[str] = field(default_factory=list)
    next_call_blocker: asyncio.Event = field(default_factory=asyncio.Event)

    async def synthesize(self, text: str) -> AsyncIterator[object]:
        self.calls.append(text)
        if len(self.calls) == 1:
            raise RuntimeError("tts failure sentinel")
        await self.next_call_blocker.wait()
        if False:
            yield _core_module().AudioSegment(1, b"", (0, 0))


@dataclass
class FirstCallFailingPersistence(RecordingPersistence):
    async def start_response(
        self, *, response_id: str, user_content: str
    ) -> ResponseStartResult:
        if not self.starts:
            self.starts.append((response_id, user_content))
            raise RuntimeError("response start failure sentinel")
        return await super().start_response(
            response_id=response_id,
            user_content=user_content,
        )


@dataclass
class FirstTerminalFailingPersistence(RecordingPersistence):
    persist_attempts: int = 0

    async def persist(self, outcome: object) -> None:
        self.persist_attempts += 1
        if self.persist_attempts == 1:
            raise RuntimeError("terminal persistence failure sentinel")
        await super().persist(outcome)


@dataclass
class BlockingStt:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancellation_received: asyncio.Event = field(default_factory=asyncio.Event)
    blocker: asyncio.Event = field(default_factory=asyncio.Event)

    async def transcribe(self, audio: bytes) -> str:
        self.started.set()
        try:
            await self.blocker.wait()
        except asyncio.CancelledError:
            self.cancellation_received.set()
            raise
        return "到達しない書き起こし"


@dataclass
class CancellationSuppressingTts:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancellation_received: asyncio.Event = field(default_factory=asyncio.Event)
    blocker: asyncio.Event = field(default_factory=asyncio.Event)

    async def synthesize(self, text: str) -> AsyncIterator[object]:
        module = _core_module()
        self.started.set()
        try:
            await self.blocker.wait()
        except asyncio.CancelledError:
            self.cancellation_received.set()
        yield module.AudioSegment(1, b"late-audio", (0, len(text)))
