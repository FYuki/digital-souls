from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib
import threading

import pytest

from tests.conversation_core_test_support import (
    BlockingLlm,
    make_pcm16_wav,
    RecordingDelivery,
    RecordingObservation,
    RecordingStt,
    RecordingTts,
    response_id_factory,
)


def _modules():
    try:
        public = importlib.import_module("app.conversation_core")
        adapters = importlib.import_module("app.conversation_core.adapters")
    except ModuleNotFoundError as error:
        if error.name in {"app.conversation_core", "app.conversation_core.adapters"}:
            pytest.fail("Conversation Core provider adapters must be implemented")
        raise
    return public, adapters


@dataclass
class FakeWhisperTranscriber:
    calls: list[bytes] = field(default_factory=list)

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        return "書き起こし"


@dataclass
class FakeVoicevoxClient:
    audio: bytes
    calls: list[str] = field(default_factory=list)

    def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        return self.audio


@dataclass
class FakePromptLlmGenerator:
    calls: list[str] = field(default_factory=list)

    def __call__(self, transcript: str) -> str:
        self.calls.append(transcript)
        return "光織からの応答🙂"


def test_whisper_adapter_exposes_transcription_as_an_independent_async_stage() -> None:
    async def exercise() -> None:
        _public, adapters = _modules()
        transcriber = FakeWhisperTranscriber()
        adapter = adapters.WhisperSttAdapter(transcriber=transcriber)

        result = await adapter.transcribe(b"input-pcm")

        assert result == "書き起こし"
        assert transcriber.calls == [b"input-pcm"]

    asyncio.run(exercise())


def test_whisper_adapter_rejects_overflow_without_interrupting_active_request() -> None:
    class BlockingTranscriber:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls: list[bytes] = []

        def transcribe(self, audio: bytes) -> str:
            self.calls.append(audio)
            self.started.set()
            assert self.release.wait(timeout=1)
            return "書き起こし"

    async def exercise() -> None:
        _public, adapters = _modules()
        transcriber = BlockingTranscriber()
        adapter = adapters.WhisperSttAdapter(
            transcriber=transcriber, max_inflight=1
        )
        first = asyncio.create_task(adapter.transcribe(b"first"))
        await asyncio.sleep(0.05)
        assert transcriber.started.is_set()

        with pytest.raises(adapters.SttCapacityError) as caught:
            await adapter.transcribe(b"overflow")
        assert caught.value.error_code == "stt_capacity_exceeded"

        transcriber.release.set()
        assert await first == "書き起こし"
        assert await adapter.transcribe(b"next") == "書き起こし"
        assert transcriber.calls == [b"first", b"next"]

    asyncio.run(exercise())


def test_voicevox_adapter_normalizes_24khz_wav_to_48khz_pcm_segment() -> None:
    async def exercise() -> None:
        _public, adapters = _modules()
        input_pcm = b"\x00\x00" * 4
        client = FakeVoicevoxClient(
            audio=make_pcm16_wav(
                pcm=input_pcm,
                sample_rate=24_000,
                channels=1,
            )
        )
        adapter = adapters.VoicevoxTtsAdapter(
            client=client,
            output_sample_rate=48_000,
            output_channels=1,
            output_sample_width=2,
        )

        segments = [segment async for segment in adapter.synthesize("光織🙂")]

        assert client.calls == ["光織🙂"]
        assert len(segments) == 1
        assert segments[0].audio_sequence == 1
        assert segments[0].audio == b"\x00\x00" * 7
        assert not segments[0].audio.startswith(b"RIFF")
        assert segments[0].text_range == (0, 3)

    asyncio.run(exercise())


def test_voicevox_adapter_removes_container_without_resampling_48khz_pcm() -> None:
    async def exercise() -> None:
        _public, adapters = _modules()
        input_pcm = b"\x01\x00\x02\x00\x03\x00"
        client = FakeVoicevoxClient(
            audio=make_pcm16_wav(
                pcm=input_pcm,
                sample_rate=48_000,
                channels=1,
            )
        )
        adapter = adapters.VoicevoxTtsAdapter(
            client=client,
            output_sample_rate=48_000,
            output_channels=1,
            output_sample_width=2,
        )

        segments = [segment async for segment in adapter.synthesize("同率")]

        assert segments[0].audio == input_pcm

    asyncio.run(exercise())


def test_voicevox_adapter_rejects_non_wav_audio_without_fallback() -> None:
    async def exercise() -> None:
        _public, adapters = _modules()
        adapter = adapters.VoicevoxTtsAdapter(
            client=FakeVoicevoxClient(audio=b"not-wav"),
            output_sample_rate=48_000,
            output_channels=1,
            output_sample_width=2,
        )

        with pytest.raises(EOFError):
            _segments = [segment async for segment in adapter.synthesize("不正")]

    asyncio.run(exercise())


def test_prompt_llm_adapter_emits_one_text_delta_without_fixing_the_port_to_one() -> None:
    async def exercise() -> None:
        _public, adapters = _modules()
        generator = FakePromptLlmGenerator()
        adapter = adapters.PromptLlmAdapter(generate_reply=generator)

        deltas = [delta async for delta in adapter.generate("利用者の発話🙂")]

        assert generator.calls == ["利用者の発話🙂"]
        assert len(deltas) == 1
        assert deltas[0].text_sequence == 1
        assert deltas[0].text == "光織からの応答🙂"
        assert deltas[0].text_range == (0, 8)

    asyncio.run(exercise())


@dataclass
class FakeHistorySession:
    @dataclass(frozen=True)
    class StartedTurn:
        handle: object
        content_skipped: bool

    started: list[str] = field(default_factory=list)
    completed: list[tuple[object, str]] = field(default_factory=list)
    interrupted: list[tuple[object, str, list[dict[str, object]], int]] = field(
        default_factory=list
    )
    failed: list[object] = field(default_factory=list)
    content_skipped: bool = False
    completed_turn: object = field(default_factory=object)

    def start_turn(self, user_content: str) -> object:
        handle = object()
        self.started.append(user_content)
        self.handle = self.StartedTurn(handle, self.content_skipped)
        return self.handle

    def complete_turn(self, started_turn: object, assistant_content: str) -> object:
        self.completed.append((started_turn, assistant_content))
        return self.completed_turn

    def interrupt_turn(
        self,
        started_turn: object,
        generated_text: str,
        response_audio_segments: list[dict[str, object]],
        last_played_audio_sequence: int,
    ) -> None:
        self.interrupted.append(
            (
                started_turn,
                generated_text,
                response_audio_segments,
                last_played_audio_sequence,
            )
        )

    def fail_turn(self, started_turn: object) -> None:
        if isinstance(started_turn, self.StartedTurn) and started_turn.content_skipped:
            return
        self.failed.append(started_turn)


@dataclass
class TerminalRecordingDelivery(RecordingDelivery):
    next_response_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def publish(self, event: object) -> None:
        await super().publish(event)
        if getattr(event, "type") == "response_started" and getattr(
            event, "generation"
        ) == 2:
            self.next_response_started.set()


def test_history_adapter_propagates_privacy_skipped_start_result() -> None:
    async def exercise() -> None:
        _public, adapters = _modules()
        history = FakeHistorySession(content_skipped=True)
        adapter = adapters.ConversationHistoryPersistenceAdapter(
            history_session=history
        )

        result = await adapter.start_response(
            response_id="50000000-0000-4000-8000-000000000903",
            user_content="保存対象外",
        )

        assert result.content_skipped is True
        assert history.started == ["保存対象外"]

    asyncio.run(exercise())


def test_cancelled_persistence_delegates_prefix_selection_to_history_session_once() -> None:
    async def exercise() -> None:
        public, adapters = _modules()
        history = FakeHistorySession()
        adapter = adapters.ConversationHistoryPersistenceAdapter(history_session=history)
        response_id = "50000000-0000-4000-8000-000000000901"
        await adapter.start_response(response_id=response_id, user_content="利用者の発話")
        outcome = public.TerminalOutcome(
            response_id=response_id,
            generation=1,
            state=public.ResponseState.CANCELLED,
            reason="barge_in",
            generated_text="一二三四",
            audio_segments=(
                public.AudioSegment(
                    audio_sequence=1,
                    audio=b"one",
                    text_range=(0, 2),
                ),
                public.AudioSegment(
                    audio_sequence=2,
                    audio=b"two",
                    text_range=(2, 4),
                ),
            ),
            last_played_audio_sequence=1,
        )

        await adapter.persist(outcome)
        await adapter.persist(outcome)

        assert history.started == ["利用者の発話"]
        assert history.completed == []
        assert history.failed == []
        assert len(history.interrupted) == 1
        handle, generated_text, segments, last_sequence = history.interrupted[0]
        assert handle is history.handle
        assert generated_text == "一二三四"
        assert segments == [
            {"audio_sequence": 1, "text_range": {"start": 0, "end": 2}},
            {"audio_sequence": 2, "text_range": {"start": 2, "end": 4}},
        ]
        assert last_sequence == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("terminal_method", "history_operation"),
    [
        ("complete_response", "completed"),
        ("cancel_response", "interrupted"),
        ("fail_response", "failed"),
        ("privacy_skip_response", "failed"),
    ],
)
def test_core_starts_and_terminates_the_same_history_turn_once(
    terminal_method: str,
    history_operation: str,
) -> None:
    async def exercise() -> None:
        public, adapters = _modules()
        history = FakeHistorySession()
        formation_candidates: list[object] = []
        persistence = adapters.ConversationHistoryPersistenceAdapter(
            history_session=history,
            completed_turn_observer=formation_candidates.append,
        )
        response_id = "50000000-0000-4000-8000-000000000902"
        session = public.ConversationCoreSession(
            session_id="20000000-0000-4000-8000-000000000902",
            response_id_factory=response_id_factory(response_id),
            delivery=RecordingDelivery(),
            persistence=persistence,
            observation=RecordingObservation(),
            stt=RecordingStt(),
            llm=BlockingLlm(),
            tts=RecordingTts(),
        )
        response = await session.finalize_utterance(
            utterance_id="30000000-0000-4000-8000-000000000902",
            transcript="履歴へ保存する利用者発話",
            should_response=True,
        )
        kwargs: dict[str, object] = {
            "response_id": response.response_id,
            "generation": response.generation,
        }
        if terminal_method == "cancel_response":
            kwargs = {"response_id": response.response_id, "reason": "barge_in"}

        first = await getattr(session, terminal_method)(**kwargs)
        second = await getattr(session, terminal_method)(**kwargs)
        await asyncio.wait_for(session.end(), timeout=0.5)

        assert first == second
        assert history.started == ["履歴へ保存する利用者発話"]
        assert len(history.completed) == (1 if history_operation == "completed" else 0)
        assert len(history.interrupted) == (
            1 if history_operation == "interrupted" else 0
        )
        assert len(history.failed) == (1 if history_operation == "failed" else 0)
        assert formation_candidates == (
            [history.completed_turn] if history_operation == "completed" else []
        )
        terminal_handles = [item[0] for item in history.completed]
        terminal_handles.extend(item[0] for item in history.interrupted)
        terminal_handles.extend(history.failed)
        assert terminal_handles == [history.handle]

    asyncio.run(exercise())


def test_history_start_failure_delivers_failed_and_starts_pending_response() -> None:
    async def exercise() -> None:
        public, _adapters = _modules()

        class FirstStartFailingPersistence:
            def __init__(self) -> None:
                self.started: list[str] = []
                self.first_start_entered = asyncio.Event()
                self.release_first_start = asyncio.Event()

            async def start_response(
                self, *, response_id: str, user_content: str
            ) -> object:
                self.started.append(user_content)
                if len(self.started) == 1:
                    self.first_start_entered.set()
                    await self.release_first_start.wait()
                    raise RuntimeError("history start failed")
                return public.ResponseStartResult(content_skipped=False)

            async def persist(self, outcome: object) -> None:
                return None

        persistence = FirstStartFailingPersistence()
        delivery = TerminalRecordingDelivery()
        first_response_id = "50000000-0000-4000-8000-000000000904"
        second_response_id = "50000000-0000-4000-8000-000000000905"
        session = public.ConversationCoreSession(
            session_id="20000000-0000-4000-8000-000000000904",
            response_id_factory=response_id_factory(
                first_response_id,
                second_response_id,
            ),
            delivery=delivery,
            persistence=persistence,
            observation=RecordingObservation(),
            stt=RecordingStt(),
            llm=BlockingLlm(),
            tts=RecordingTts(),
        )

        first_finalize = asyncio.create_task(
            session.finalize_utterance(
                utterance_id="30000000-0000-4000-8000-000000000904",
                transcript="最初の発話",
                should_response=True,
            )
        )
        await asyncio.wait_for(persistence.first_start_entered.wait(), timeout=0.5)
        try:
            await session.finalize_utterance(
                utterance_id="30000000-0000-4000-8000-000000000905",
                transcript="次の発話",
                should_response=True,
            )
        finally:
            persistence.release_first_start.set()

        first_response = await first_finalize
        await asyncio.wait_for(delivery.next_response_started.wait(), timeout=0.5)

        failed_events = [
            event for event in delivery.events if getattr(event, "type") == "response_failed"
        ]
        assert first_response is not None
        assert first_response.response_id == first_response_id
        assert first_response.state is public.ResponseState.FAILED
        assert len(failed_events) == 1
        assert failed_events[0].response_id == first_response_id
        assert session.active_response is not None
        assert session.active_response.response_id == second_response_id
        assert session.active_response.state is public.ResponseState.IN_PROGRESS
        assert session.active_response.source_utterance_ids == (
            "30000000-0000-4000-8000-000000000905",
        )
        assert persistence.started == ["最初の発話", "次の発話"]

        await asyncio.wait_for(session.end(), timeout=0.5)

    asyncio.run(exercise())
