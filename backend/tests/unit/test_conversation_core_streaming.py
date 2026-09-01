from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.conversation_core import (
    AudioSegment,
    ConversationCoreSession,
    TextDelta,
)
from app.conversation_core.segmentation import JapaneseTextSegmenter, TextSegment
from tests.conversation_core_test_support import (
    RecordingDelivery,
    RecordingObservation,
    RecordingPersistence,
    RecordingStt,
    event_field,
    response_id_factory,
)


def test_japanese_segmenter_confirms_sentence_clause_and_final_remainder() -> None:
    segmenter = JapaneseTextSegmenter(max_chars=20, min_clause_chars=6)

    assert segmenter.feed("今日は晴れです。次は少し長い節、") == (
        TextSegment("今日は晴れです。", (0, 8)),
        TextSegment("次は少し長い節、", (8, 16)),
    )
    assert segmenter.feed("終端なし") == ()
    assert segmenter.finish()[0].text == "終端なし"


def test_japanese_segmenter_drops_whitespace_only_sentence_before_tts() -> None:
    segmenter = JapaneseTextSegmenter()

    assert segmenter.feed("\n") == ()
    assert segmenter.feed("次です。") == (
        TextSegment(text="次です。", text_range=(1, 5)),
    )


def test_japanese_segmenter_does_not_split_a_short_url_at_soft_limit() -> None:
    segmenter = JapaneseTextSegmenter(max_chars=12, min_clause_chars=4)
    segments = segmenter.feed("参照 https://example.com/path 続き")

    combined = "".join(segment.text for segment in (*segments, *segmenter.finish()))
    assert "https://example.com/path" in combined
    assert not any(segment.text.endswith("https:") for segment in segments)


def test_japanese_segmenter_bounds_long_text_without_punctuation_and_finishes_remainder() -> None:
    segmenter = JapaneseTextSegmenter(max_chars=12, min_clause_chars=4)

    first = segmenter.feed("区切りのない長い日本語テキストがそのまま続く")
    remainder = segmenter.finish()

    assert first
    assert all(segment.text for segment in (*first, *remainder))
    assert "".join(segment.text for segment in (*first, *remainder)) == (
        "区切りのない長い日本語テキストがそのまま続く"
    )
    segments = (*first, *remainder)
    assert segments[0].text_range[0] == 0
    assert all(
        previous.text_range[1] == following.text_range[0]
        for previous, following in zip(segments, segments[1:], strict=False)
    )


@dataclass
class ControlledStreamingLlm:
    first_delta_emitted: asyncio.Event = field(default_factory=asyncio.Event)
    release_remainder: asyncio.Event = field(default_factory=asyncio.Event)

    async def generate(self, _transcript: str) -> AsyncIterator[TextDelta]:
        yield TextDelta(1, "最初の文です。", (0, 7))
        self.first_delta_emitted.set()
        await self.release_remainder.wait()
        yield TextDelta(2, "続きです。", (7, 12))


@dataclass
class RecordingSegmentTts:
    first_audio_ready: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[str] = field(default_factory=list)

    async def synthesize(self, text: str) -> AsyncIterator[AudioSegment]:
        self.calls.append(text)
        self.first_audio_ready.set()
        yield AudioSegment(1, text.encode(), (0, len(text)))


def test_first_audio_is_delivered_before_llm_stream_completes() -> None:
    async def exercise() -> None:
        llm = ControlledStreamingLlm()
        tts = RecordingSegmentTts()
        delivery = RecordingDelivery()
        session = ConversationCoreSession(
            session_id="20000000-0000-4000-8000-000000000001",
            response_id_factory=response_id_factory(
                "50000000-0000-4000-8000-000000000001"
            ),
            delivery=delivery,
            persistence=RecordingPersistence(),
            observation=RecordingObservation(),
            stt=RecordingStt(),
            llm=llm,
            tts=tts,
            tts_queue_maxsize=1,
        )
        await session.finalize_utterance(
            utterance_id="30000000-0000-4000-8000-000000000001",
            transcript="話して",
            should_response=True,
        )
        await asyncio.wait_for(tts.first_audio_ready.wait(), timeout=0.5)
        while not any(
            event_field(event, "type") == "response_audio_segment"
            for event in delivery.events
        ):
            await asyncio.sleep(0)

        assert llm.release_remainder.is_set() is False
        assert [
            event_field(event, "type") for event in delivery.events
        ] == ["response_started", "response_delta", "response_audio_segment"]

        llm.release_remainder.set()
        while session.running_stage_count:
            await asyncio.sleep(0)
        assert tts.calls == ["最初の文です。", "続きです。"]
        assert session.active_response is None

    asyncio.run(exercise())


def test_bounded_tts_queue_applies_backpressure_to_llm_stream() -> None:
    @dataclass
    class BurstLlm:
        yielded: int = 0

        async def generate(self, _transcript: str) -> AsyncIterator[TextDelta]:
            offset = 0
            for sequence in range(1, 21):
                text = f"文{sequence}。"
                self.yielded += 1
                yield TextDelta(sequence, text, (offset, offset + len(text)))
                offset += len(text)

    @dataclass
    class BlockingTts:
        entered: asyncio.Event = field(default_factory=asyncio.Event)
        release: asyncio.Event = field(default_factory=asyncio.Event)

        async def synthesize(self, text: str) -> AsyncIterator[AudioSegment]:
            self.entered.set()
            await self.release.wait()
            yield AudioSegment(1, text.encode(), (0, len(text)))

    async def exercise() -> None:
        llm = BurstLlm()
        tts = BlockingTts()
        session = ConversationCoreSession(
            session_id="20000000-0000-4000-8000-000000000002",
            response_id_factory=response_id_factory(
                "50000000-0000-4000-8000-000000000002"
            ),
            delivery=RecordingDelivery(),
            persistence=RecordingPersistence(),
            observation=RecordingObservation(),
            stt=RecordingStt(),
            llm=llm,
            tts=tts,
            tts_queue_maxsize=1,
        )
        response = await session.finalize_utterance(
            utterance_id="30000000-0000-4000-8000-000000000002",
            transcript="連続生成して",
            should_response=True,
        )
        await asyncio.wait_for(tts.entered.wait(), timeout=0.5)
        for _ in range(10):
            await asyncio.sleep(0)

        assert llm.yielded == 3

        await session.cancel_response(
            response_id=response.response_id,
            reason="test_cancel",
        )
        tts.release.set()
        await session.end()

    asyncio.run(exercise())
