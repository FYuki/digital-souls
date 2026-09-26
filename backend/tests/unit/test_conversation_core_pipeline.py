from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from app.conversation_core.models import AudioSegment, Response, ResponseState, TextDelta


def _pipeline_module():
    try:
        return importlib.import_module("app.conversation_core.pipeline")
    except ModuleNotFoundError as error:
        if error.name == "app.conversation_core.pipeline":
            pytest.fail("Conversation Core pipeline boundary is not implemented")
        raise


@dataclass
class BlockingLlm:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def generate(self, _transcript: str, *, response: object | None = None) -> AsyncIterator[TextDelta]:
        self.started.set()
        yield TextDelta(1, "開始。", (0, 3))
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@dataclass
class BlockingTts:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def synthesize(self, _text: str) -> AsyncIterator[AudioSegment]:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield AudioSegment(1, b"audio", (0, 3))


def test_pipeline_completes_only_after_text_and_audio_consumers_finish():
    async def scenario() -> None:
        module = _pipeline_module()
        response = Response(
            response_id="response-1",
            generation=1,
            source_utterance_ids=("utterance-1",),
            state=ResponseState.IN_PROGRESS,
        )
        accepted: list[str] = []
        completed = False

        class Llm:
            async def generate(self, _transcript: str, *, response: object | None = None) -> AsyncIterator[TextDelta]:
                yield TextDelta(1, "結果。", (0, 3))

        class Tts:
            async def synthesize(self, _text: str) -> AsyncIterator[AudioSegment]:
                yield AudioSegment(1, b"audio", (0, 3))

        async def accept_text_delta(**_kwargs: object) -> bool:
            accepted.append("text")
            return True

        async def accept_audio_segment(**_kwargs: object) -> bool:
            accepted.append("audio")
            return True

        async def complete_response(_response: Response) -> None:
            nonlocal completed
            completed = True
            assert accepted == ["text", "audio"]

        await module.run_response_pipeline(
            response=response,
            response_input="入力",
            llm=Llm(),
            tts=Tts(),
            tts_queue_maxsize=1,
            accept_text_delta=accept_text_delta,
            accept_audio_segment=accept_audio_segment,
            is_response_current=lambda: True,
            complete_response=complete_response,
        )

        assert completed

    asyncio.run(scenario())


def test_pipeline_cancellation_collects_both_provider_consumers():
    async def scenario() -> None:
        module = _pipeline_module()
        llm, tts = BlockingLlm(), BlockingTts()
        response = Response(
            response_id="response-1",
            generation=1,
            source_utterance_ids=("utterance-1",),
            state=ResponseState.IN_PROGRESS,
        )

        async def accept_text_delta(**_kwargs: object) -> bool:
            return True

        async def accept_audio_segment(**_kwargs: object) -> bool:
            return True

        task = asyncio.create_task(
            module.run_response_pipeline(
                response=response,
                response_input="入力",
                llm=llm,
                tts=tts,
                tts_queue_maxsize=1,
                accept_text_delta=accept_text_delta,
                accept_audio_segment=accept_audio_segment,
                is_response_current=lambda: True,
                complete_response=lambda _response: asyncio.sleep(0),
            )
        )
        await asyncio.wait_for(llm.started.wait(), 1)
        await asyncio.wait_for(tts.started.wait(), 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert llm.cancelled.is_set()
        assert tts.cancelled.is_set()

    asyncio.run(scenario())
