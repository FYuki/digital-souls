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

    async def generate(self, transcript: str) -> AsyncIterator[object]:
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

    async def generate(self, transcript: str) -> AsyncIterator[object]:
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


def test_public_audio_entry_runs_stt_llm_tts_delivery_and_completion() -> None:
    async def exercise() -> None:
        module = _core_module()
        stt = RecordingStt(transcript="利用者の発話")
        llm = RecordingLlm(
            deltas=(
                module.TextDelta(1, "光織", (0, 2)),
                module.TextDelta(2, "です", (2, 4)),
            )
        )
        tts = RecordingTts()
        module, session, delivery, persistence, observation = _automatic_session(
            stt=stt,
            llm=llm,
            tts=tts,
        )

        response = await session.start_transcription(
            utterance_id=UTTERANCE_1,
            audio=b"input-audio",
            should_response=True,
        )
        await _wait_until(lambda: session.running_stage_count == 0)

        assert stt.calls == [b"input-audio"]
        assert llm.calls == ["利用者の発話"]
        assert tts.calls == ["光織です"]
        assert response.state is module.ResponseState.IN_PROGRESS
        assert session.response(response.response_id).state is module.ResponseState.COMPLETED
        assert session.response(response.response_id).last_text_sequence == 2
        assert persistence.starts == [(RESPONSE_1, "利用者の発話")]
        assert len(persistence.outcomes) == 1
        assert [event_field(event, "type") for event in delivery.events] == [
            "utterance_finalized",
            "response_started",
            "response_delta",
            "response_delta",
            "response_audio_segment",
            "response_completed",
        ]
        assert event_field(delivery.events[-1], "last_text_sequence") == 2
        producer_observations = [
            item
            for item in observation.observations
            if event_field(item, "stage") != "delivery"
        ]
        assert [event_field(item, "stage") for item in producer_observations] == [
            "stt",
            "stt",
            "llm",
            "llm",
            "tts",
            "tts",
        ]
        assert [event_field(item, "outcome") for item in producer_observations] == [
            "started",
            "completed",
            "started",
            "completed",
            "started",
            "completed",
        ]
        assert event_field(observation.observations[0], "utterance_id") == UTTERANCE_1
        assert event_field(observation.observations[0], "response_id") is None

    _run(exercise)


def test_automatic_llm_cancel_propagates_and_late_result_is_gated() -> None:
    async def exercise() -> None:
        llm = CancellationSuppressingLlm()
        module, session, delivery, persistence, observation = _automatic_session(
            stt=RecordingStt(),
            llm=llm,
            tts=RecordingTts(),
        )
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="中断対象",
            should_response=True,
        )
        await llm.started.wait()

        cancelled = await session.cancel_response(
            response_id=response.response_id,
            reason="barge_in",
        )
        await llm.cancellation_received.wait()
        await _wait_until(lambda: session.running_stage_count == 0)

        assert cancelled.state is module.ResponseState.CANCELLED
        assert len(persistence.outcomes) == 1
        assert [
            event_field(event, "type")
            for event in delivery.events
            if event_field(event, "type") in {"response_delta", "response_audio_segment"}
        ] == []
        llm_observations = [
            item
            for item in observation.observations
            if event_field(item, "stage") == "llm"
        ]
        assert [event_field(item, "outcome") for item in llm_observations] == [
            "started",
            "cancelled",
        ]

    _run(exercise)


def test_automatic_tts_cancel_propagates_and_late_segment_is_gated() -> None:
    async def exercise() -> None:
        module = _core_module()
        tts = CancellationSuppressingTts()
        module, session, delivery, persistence, observation = _automatic_session(
            stt=RecordingStt(),
            llm=RecordingLlm(deltas=(module.TextDelta(1, "応答", (0, 2)),)),
            tts=tts,
        )
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="中断対象",
            should_response=True,
        )
        await tts.started.wait()
        delta_count_before_cancel = sum(
            event_field(event, "type") == "response_delta" for event in delivery.events
        )

        await session.cancel_response(
            response_id=response.response_id,
            reason="barge_in",
        )
        await tts.cancellation_received.wait()
        await _wait_until(lambda: session.running_stage_count == 0)

        assert delta_count_before_cancel == 1
        assert len(persistence.outcomes) == 1
        assert [
            event_field(event, "type")
            for event in delivery.events
            if event_field(event, "type") == "response_audio_segment"
        ] == []
        tts_observations = [
            item
            for item in observation.observations
            if event_field(item, "stage") == "tts"
        ]
        assert [event_field(item, "outcome") for item in tts_observations] == [
            "started",
            "cancelled",
        ]

    _run(exercise)


def test_automatic_llm_failure_terminates_once_and_starts_pending_response() -> None:
    async def exercise() -> None:
        llm = FirstCallFailingLlm()
        module, session, delivery, persistence, _observation = _automatic_session(
            stt=RecordingStt(),
            llm=llm,
            tts=RecordingTts(),
        )
        first = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="失敗する入力",
            should_response=True,
        )
        await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="次の入力",
            should_response=True,
        )

        await _wait_until(
            lambda: session.active_response is not None
            and session.active_response.response_id == RESPONSE_2
        )

        assert session.response(first.response_id).state is module.ResponseState.FAILED
        assert session.active_response.state is module.ResponseState.IN_PROGRESS
        assert persistence.starts == [
            (RESPONSE_1, "失敗する入力"),
            (RESPONSE_2, "次の入力"),
        ]
        assert len(persistence.outcomes) == 1
        assert [event_field(event, "type") for event in _terminal_events(delivery)] == [
            "response_failed"
        ]
        await session.end()

    _run(exercise)


def test_response_start_failure_terminates_once_and_allows_the_next_response() -> None:
    async def exercise() -> None:
        module = _core_module()
        delivery = RecordingDelivery()
        persistence = FirstCallFailingPersistence()
        session = module.ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(RESPONSE_1, RESPONSE_2),
            delivery=delivery,
            persistence=persistence,
            observation=RecordingObservation(),
            stt=RecordingStt(),
            llm=BlockingLlm(),
            tts=RecordingTts(),
        )

        first = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="開始に失敗する入力",
            should_response=True,
        )

        assert first.state is module.ResponseState.FAILED
        assert session.response(first.response_id).state is module.ResponseState.FAILED
        assert session.active_response is None

        second = await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="失敗後の入力",
            should_response=True,
        )
        await _wait_until(lambda: len(_terminal_events(delivery)) == 1)

        assert second.response_id == RESPONSE_2
        assert session.active_response == second
        assert second.state is module.ResponseState.IN_PROGRESS
        assert persistence.starts == [
            (RESPONSE_1, "開始に失敗する入力"),
            (RESPONSE_2, "失敗後の入力"),
        ]
        assert len(persistence.outcomes) == 1
        assert (
            event_field(persistence.outcomes[0], "state")
            is module.ResponseState.FAILED
        )
        assert [event_field(event, "type") for event in _terminal_events(delivery)] == [
            "response_failed"
        ]
        await session.end()

    _run(exercise)


def test_terminal_persistence_failure_still_delivers_and_starts_pending_response() -> None:
    async def exercise() -> None:
        module = _core_module()
        delivery = RecordingDelivery()
        persistence = FirstTerminalFailingPersistence()
        session = module.ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(RESPONSE_1, RESPONSE_2),
            delivery=delivery,
            persistence=persistence,
            observation=RecordingObservation(),
            stt=RecordingStt(),
            llm=BlockingLlm(),
            tts=RecordingTts(),
        )
        first = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="永続化に失敗する入力",
            should_response=True,
        )
        await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="後続の入力",
            should_response=True,
        )

        await session.complete_response(
            response_id=first.response_id,
            generation=first.generation,
        )
        await _wait_until(lambda: len(_response_started_events(delivery)) == 2)

        assert persistence.persist_attempts == 1
        assert [event_field(event, "type") for event in _terminal_events(delivery)] == [
            "response_completed"
        ]
        assert session.active_response is not None
        assert session.active_response.response_id == RESPONSE_2
        await session.end()

    _run(exercise)


def test_automatic_tts_failure_terminates_once_and_starts_pending_response() -> None:
    async def exercise() -> None:
        module = _core_module()
        tts = FirstCallFailingTts()
        module, session, delivery, persistence, observation = _automatic_session(
            stt=RecordingStt(),
            llm=RecordingLlm(
                deltas=(module.TextDelta(1, "応答", (0, 2)),),
            ),
            tts=tts,
        )
        first = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="失敗する入力",
            should_response=True,
        )
        await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="次の入力",
            should_response=True,
        )

        await _wait_until(
            lambda: session.active_response is not None
            and session.active_response.response_id == RESPONSE_2
        )

        assert session.response(first.response_id).state is module.ResponseState.FAILED
        assert session.active_response.state is module.ResponseState.IN_PROGRESS
        assert persistence.starts == [
            (RESPONSE_1, "失敗する入力"),
            (RESPONSE_2, "次の入力"),
        ]
        assert len(persistence.outcomes) == 1
        assert [event_field(event, "type") for event in _terminal_events(delivery)] == [
            "response_failed"
        ]
        first_tts_observations = [
            item
            for item in observation.observations
            if event_field(item, "response_id") == RESPONSE_1
            and event_field(item, "stage") == "tts"
        ]
        assert [event_field(item, "outcome") for item in first_tts_observations] == [
            "started",
            "failed",
        ]
        await session.end()

    _run(exercise)


def test_disconnect_cancels_and_collects_an_automatic_stt_task() -> None:
    async def exercise() -> None:
        stt = BlockingStt()
        _module, session, _delivery, persistence, observation = _automatic_session(
            stt=stt,
            llm=BlockingLlm(),
            tts=RecordingTts(),
        )
        task = session.start_transcription(
            utterance_id=UTTERANCE_1,
            audio=b"input-audio",
            should_response=True,
        )
        await stt.started.wait()

        await session.disconnect()

        assert task.cancelled()
        assert stt.cancellation_received.is_set()
        assert session.running_stage_count == 0
        assert persistence.starts == []
        assert [event_field(item, "outcome") for item in observation.observations] == [
            "started",
            "cancelled",
        ]

    _run(exercise)


def test_response_id_is_unique_and_source_utterances_are_consumed_once() -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, _observation = _session()

        first = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="最初",
            should_response=True,
        )
        await session.complete_response(
            response_id=first.response_id,
            generation=first.generation,
        )
        second = await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="次",
            should_response=True,
        )

        assert first.response_id == RESPONSE_1
        assert second.response_id == RESPONSE_2
        assert first.response_id != second.response_id
        assert first.source_utterance_ids == (UTTERANCE_1,)
        assert second.source_utterance_ids == (UTTERANCE_2,)
        assert session.response(RESPONSE_1).state is module.ResponseState.COMPLETED
        assert session.response(RESPONSE_2).state is module.ResponseState.IN_PROGRESS
        assert [
            event_field(event, "source_utterance_ids")
            for event in _response_started_events(delivery)
        ] == [(UTTERANCE_1,), (UTTERANCE_2,)]
        assert len(persistence.outcomes) == 1

    _run(exercise)


def test_active_response_keeps_all_new_utterances_pending_without_cancelling() -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, _observation = _session()
        active = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="応答を始めて",
            should_response=True,
        )

        pending_1 = await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="続き一",
            should_response=True,
        )
        pending_2 = await session.finalize_utterance(
            utterance_id=UTTERANCE_3,
            transcript="続き二",
            should_response=True,
        )

        assert pending_1 is None
        assert pending_2 is None
        assert session.active_response.response_id == active.response_id
        assert session.active_response.state is module.ResponseState.IN_PROGRESS
        assert tuple(item.utterance_id for item in session.pending_utterances) == (
            UTTERANCE_2,
            UTTERANCE_3,
        )
        assert _terminal_events(delivery) == []
        assert persistence.outcomes == []

    _run(exercise)


def test_terminal_response_starts_one_response_from_all_pending_utterances() -> None:
    async def exercise() -> None:
        _module, session, delivery, _persistence, _observation = _session()
        first = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="開始",
            should_response=True,
        )
        await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="保留一",
            should_response=False,
        )
        await session.finalize_utterance(
            utterance_id=UTTERANCE_3,
            transcript="保留二",
            should_response=True,
        )

        await session.complete_response(
            response_id=first.response_id,
            generation=first.generation,
        )
        await _wait_until(lambda: len(_response_started_events(delivery)) == 2)

        next_response = session.active_response
        assert next_response.response_id == RESPONSE_2
        assert next_response.source_utterance_ids == (UTTERANCE_2, UTTERANCE_3)
        assert session.pending_utterances == ()
        assert [
            event_field(event, "source_utterance_ids")
            for event in _response_started_events(delivery)
        ] == [(UTTERANCE_1,), (UTTERANCE_2, UTTERANCE_3)]

    _run(exercise)


@pytest.mark.parametrize(
    "terminal_method",
    ["cancel_response", "fail_response", "privacy_skip_response"],
)
def test_each_non_completed_terminal_starts_the_pending_response(
    terminal_method: str,
) -> None:
    async def exercise() -> None:
        _module, session, delivery, _persistence, _observation = _session()
        first = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="最初の応答",
            should_response=True,
        )
        await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="終端後に応答",
            should_response=True,
        )
        kwargs: dict[str, object] = {
            "response_id": first.response_id,
            "generation": first.generation,
        }
        if terminal_method == "cancel_response":
            kwargs = {
                "response_id": first.response_id,
                "reason": "barge_in",
            }

        await getattr(session, terminal_method)(**kwargs)
        await _wait_until(lambda: len(_response_started_events(delivery)) == 2)

        next_response = session.active_response
        assert next_response.response_id == RESPONSE_2
        assert next_response.source_utterance_ids == (UTTERANCE_2,)
        assert session.pending_utterances == ()
        assert [
            event_field(event, "source_utterance_ids")
            for event in _response_started_events(delivery)
        ] == [(UTTERANCE_1,), (UTTERANCE_2,)]

    _run(exercise)


def test_repeated_cancel_is_immediate_and_idempotent() -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="中断して",
            should_response=True,
        )

        first = await session.cancel_response(
            response_id=response.response_id,
            reason="barge_in",
        )
        second = await session.cancel_response(
            response_id=response.response_id,
            reason="barge_in",
        )

        assert first == second
        assert first.state is module.ResponseState.CANCELLED
        assert session.active_response is None
        assert [event_field(event, "type") for event in _terminal_events(delivery)] == [
            "response_cancelled"
        ]
        assert len(persistence.outcomes) == 1
        assert event_field(persistence.outcomes[0], "state") is module.ResponseState.CANCELLED

    _run(exercise)


def test_unknown_response_control_is_ignored_idempotently() -> None:
    async def exercise() -> None:
        _module, session, delivery, persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="処理中の入力",
            should_response=True,
        )

        cancelled = await session.cancel_response(
            response_id="50000000-0000-4000-8000-000000000999",
            reason="barge_in",
        )
        playback_confirmed = await session.confirm_playback(
            response_id="50000000-0000-4000-8000-000000000999",
            last_played_audio_sequence=1,
        )

        assert cancelled is None
        assert playback_confirmed is False
        assert session.active_response == response
        assert _terminal_events(delivery) == []
        assert persistence.outcomes == []
        await session.end()

    _run(exercise)


def test_cancel_for_a_terminal_response_does_not_change_the_active_response() -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, _observation = _session()
        first = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="最初の応答",
            should_response=True,
        )
        await session.complete_response(
            response_id=first.response_id,
            generation=first.generation,
        )
        second = await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="現在の応答",
            should_response=True,
        )
        terminal_events_before_cancel = list(_terminal_events(delivery))
        persisted_before_cancel = list(persistence.outcomes)

        await session.cancel_response(
            response_id=first.response_id,
            reason="barge_in",
        )

        assert session.response(first.response_id).state is module.ResponseState.COMPLETED
        assert session.active_response.response_id == second.response_id
        assert session.active_response.state is module.ResponseState.IN_PROGRESS
        assert _terminal_events(delivery) == terminal_events_before_cancel
        assert persistence.outcomes == persisted_before_cancel

    _run(exercise)


def test_cancel_finishes_before_a_running_stage_and_propagates_task_cancellation() -> None:
    async def exercise() -> None:
        module, session, _delivery, _persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="段階処理中",
            should_response=True,
        )
        stage_started = asyncio.Event()
        stage_cancelled = asyncio.Event()
        never_finishes = asyncio.Event()

        session.start_stage(
            response_id=response.response_id,
            generation=response.generation,
            stage="manual",
            operation=_blocked_until_cancelled(
                started=stage_started,
                cancelled=stage_cancelled,
                blocker=never_finishes,
            ),
        )
        await stage_started.wait()

        outcome = await asyncio.wait_for(
            session.cancel_response(
                response_id=response.response_id,
                reason="barge_in",
            ),
            timeout=0.5,
        )
        await stage_cancelled.wait()

        assert outcome.state is module.ResponseState.CANCELLED
        assert never_finishes.is_set() is False
        assert session.running_stage_count == 0

    _run(exercise)


def test_cancel_does_not_wait_for_a_stage_that_continues_after_cancellation() -> None:
    async def exercise() -> None:
        module, session, _delivery, _persistence, observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="段階処理を待たずに中断",
            should_response=True,
        )
        stage_started = asyncio.Event()
        cancellation_received = asyncio.Event()
        release_stage = asyncio.Event()

        async def continue_after_cancellation() -> None:
            stage_started.set()
            try:
                await release_stage.wait()
            except asyncio.CancelledError:
                cancellation_received.set()
                await release_stage.wait()

        stage_task = session.start_stage(
            response_id=response.response_id,
            generation=response.generation,
            stage="manual",
            operation=continue_after_cancellation(),
        )
        await stage_started.wait()

        outcome = await asyncio.wait_for(
            session.cancel_response(
                response_id=response.response_id,
                reason="barge_in",
            ),
            timeout=0.5,
        )
        await cancellation_received.wait()

        assert outcome.state is module.ResponseState.CANCELLED
        assert stage_task.done() is False
        assert session.running_stage_count == 1

        release_stage.set()
        await stage_task
        assert session.running_stage_count == 0
        manual_observations = [
            item
            for item in observation.observations
            if event_field(item, "stage") == "manual"
        ]
        assert [event_field(item, "outcome") for item in manual_observations] == [
            "started",
            "cancelled",
        ]

    _run(exercise)


def test_stage_failure_terminates_response_once_and_allows_the_next_response() -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="失敗する段階",
            should_response=True,
        )

        async def fail_stage() -> None:
            raise RuntimeError("stage failure sentinel")

        stage_task = session.start_stage(
            response_id=response.response_id,
            generation=response.generation,
            stage="manual",
            operation=fail_stage(),
        )

        with pytest.raises(RuntimeError, match="stage failure sentinel"):
            await stage_task
        next_response = await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="失敗後の発話",
            should_response=True,
        )

        assert session.response(response.response_id).state is module.ResponseState.FAILED
        assert next_response.response_id == RESPONSE_2
        assert next_response.state is module.ResponseState.IN_PROGRESS
        assert [event_field(event, "type") for event in _terminal_events(delivery)] == [
            "response_failed"
        ]
        assert len(persistence.outcomes) == 1
        assert event_field(persistence.outcomes[0], "state") is module.ResponseState.FAILED
        manual_observations = [
            item
            for item in observation.observations
            if event_field(item, "stage") == "manual"
        ]
        assert [event_field(item, "outcome") for item in manual_observations] == [
            "started",
            "failed",
        ]

    _run(exercise)


@pytest.mark.parametrize(
    ("first_terminal", "expected_state", "expected_event"),
    [
        ("cancel", "CANCELLED", "response_cancelled"),
        ("complete", "COMPLETED", "response_completed"),
    ],
)
def test_first_terminal_wins_for_state_event_persistence_and_delivery(
    first_terminal: str,
    expected_state: str,
    expected_event: str,
) -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="競合",
            should_response=True,
        )
        generation = response.generation

        cancel_allowed = asyncio.Event()
        complete_allowed = asyncio.Event()
        winner_finished = asyncio.Event()

        async def cancel() -> None:
            await cancel_allowed.wait()
            await session.cancel_response(
                response_id=response.response_id,
                reason="barge_in",
            )
            winner_finished.set()

        async def complete() -> None:
            await complete_allowed.wait()
            await session.complete_response(
                response_id=response.response_id,
                generation=generation,
            )
            winner_finished.set()

        cancel_task = asyncio.create_task(cancel())
        complete_task = asyncio.create_task(complete())
        if first_terminal == "cancel":
            cancel_allowed.set()
            await winner_finished.wait()
            complete_allowed.set()
        else:
            complete_allowed.set()
            await winner_finished.wait()
            cancel_allowed.set()
        await asyncio.gather(cancel_task, complete_task)

        state = getattr(module.ResponseState, expected_state)
        assert session.response(response.response_id).state is state
        assert [event_field(event, "type") for event in _terminal_events(delivery)] == [
            expected_event
        ]
        assert len(persistence.outcomes) == 1
        assert event_field(persistence.outcomes[0], "state") is state
        assert event_field(persistence.outcomes[0], "response_id") == response.response_id

    _run(exercise)


def test_cancelled_generation_drops_late_text_and_audio_before_both_ports() -> None:
    async def exercise() -> None:
        _module, session, delivery, persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="遅延",
            should_response=True,
        )
        generation = response.generation
        await session.cancel_response(response_id=response.response_id, reason="barge_in")
        delivered_before_late_results = list(delivery.events)
        persisted_before_late_results = list(persistence.outcomes)

        text_accepted = await session.accept_text_delta(
            response_id=response.response_id,
            generation=generation,
            text_sequence=1,
            text="届かない",
            text_range=(0, 4),
        )
        audio_accepted = await session.accept_audio_segment(
            response_id=response.response_id,
            generation=generation,
            audio_sequence=1,
            audio=b"late-audio",
            text_range=(0, 4),
        )
        await session.complete_response(
            response_id=response.response_id,
            generation=generation,
        )

        assert text_accepted is False
        assert audio_accepted is False
        assert delivery.events == delivered_before_late_results
        assert persistence.outcomes == persisted_before_late_results

    _run(exercise)


def test_terminal_generation_drops_conflicting_duplicate_before_validation() -> None:
    async def exercise() -> None:
        _module, session, delivery, persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="遅延 duplicate",
            should_response=True,
        )
        await session.accept_text_delta(
            response_id=response.response_id,
            generation=response.generation,
            event_id="70000000-0000-4000-8000-000000000107",
            text_sequence=1,
            text="確定済み",
            text_range=(0, 4),
        )
        await session.cancel_response(
            response_id=response.response_id,
            reason="barge_in",
        )
        delivered_before_late_result = list(delivery.events)
        persisted_before_late_result = list(persistence.outcomes)

        accepted = await session.accept_text_delta(
            response_id=response.response_id,
            generation=response.generation,
            event_id="70000000-0000-4000-8000-000000000107",
            text_sequence=1,
            text="競合 payload",
            text_range=(0, 10),
        )

        assert accepted is False
        assert delivery.events == delivered_before_late_result
        assert persistence.outcomes == persisted_before_late_result

    _run(exercise)


def test_audio_text_ranges_must_form_a_contiguous_prefix_before_delivery() -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="音声範囲",
            should_response=True,
        )
        await session.accept_text_delta(
            response_id=response.response_id,
            generation=response.generation,
            text_sequence=1,
            text="一二三四",
            text_range=(0, 4),
        )
        delivered_before_invalid_segment = list(delivery.events)

        with pytest.raises(module.TerminalProtocolError, match="not contiguous"):
            await session.accept_audio_segment(
                response_id=response.response_id,
                generation=response.generation,
                audio_sequence=1,
                audio=b"invalid-gap",
                text_range=(1, 3),
            )

        assert delivery.events == delivered_before_invalid_segment
        assert persistence.outcomes == []
        assert session.response(response.response_id).audio_segments == ()

    _run(exercise)


def test_audio_text_range_absorbs_skipped_whitespace_into_contiguous_prefix() -> None:
    async def exercise() -> None:
        _module, session, delivery, _persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="空白を含む音声範囲",
            should_response=True,
        )
        await session.accept_text_delta(
            response_id=response.response_id,
            generation=response.generation,
            text_sequence=1,
            text="一文目。\n二文目。",
            text_range=(0, 9),
        )
        await session.accept_audio_segment(
            response_id=response.response_id,
            generation=response.generation,
            audio_sequence=1,
            audio=b"first",
            text_range=(0, 4),
        )

        accepted = await session.accept_audio_segment(
            response_id=response.response_id,
            generation=response.generation,
            audio_sequence=2,
            audio=b"second",
            text_range=(5, 9),
        )

        assert accepted is True
        assert session.response(response.response_id).audio_segments[-1].text_range == (
            4,
            9,
        )
        assert delivery.events[-1].text_range == (4, 9)

    _run(exercise)


@pytest.mark.parametrize(
    ("terminal_method", "expected_state"),
    [
        ("complete_response", "COMPLETED"),
        ("cancel_response", "CANCELLED"),
        ("fail_response", "FAILED"),
        ("privacy_skip_response", "PRIVACY_SKIPPED"),
    ],
)
def test_each_terminal_state_is_persisted_exactly_once(
    terminal_method: str,
    expected_state: str,
) -> None:
    async def exercise() -> None:
        module, session, _delivery, persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="終端",
            should_response=True,
        )
        kwargs: dict[str, object] = {"response_id": response.response_id}
        if terminal_method != "cancel_response":
            kwargs["generation"] = response.generation
        else:
            kwargs["reason"] = "barge_in"

        first = await getattr(session, terminal_method)(**kwargs)
        second = await getattr(session, terminal_method)(**kwargs)

        state = getattr(module.ResponseState, expected_state)
        assert first == second
        assert first.state is state
        assert len(persistence.outcomes) == 1
        assert event_field(persistence.outcomes[0], "state") is state

    _run(exercise)


def test_disconnect_terminates_active_discards_pending_and_collects_tasks() -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="処理中",
            should_response=True,
        )
        await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="保留",
            should_response=True,
        )
        stage_started = asyncio.Event()
        stage_cancelled = asyncio.Event()
        never_finishes = asyncio.Event()
        session.start_stage(
            response_id=response.response_id,
            generation=response.generation,
            stage="tts",
            operation=_blocked_until_cancelled(
                started=stage_started,
                cancelled=stage_cancelled,
                blocker=never_finishes,
            ),
        )
        await stage_started.wait()

        await session.disconnect()

        assert session.response(response.response_id).state is module.ResponseState.CANCELLED
        assert session.active_response is None
        assert session.pending_utterances == ()
        assert session.utterance(UTTERANCE_2).state is module.UtteranceState.DISCARDED
        assert session.utterance(UTTERANCE_2).discard_reason == "disconnect"
        assert stage_cancelled.is_set() is True
        assert never_finishes.is_set() is False
        assert session.running_stage_count == 0
        assert [event_field(event, "type") for event in _terminal_events(delivery)] == [
            "response_cancelled"
        ]
        assert len(persistence.outcomes) == 1

    _run(exercise)


def test_end_terminates_active_discards_pending_as_session_ended_and_collects_tasks() -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, _observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="処理中",
            should_response=True,
        )
        await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="明示終了まで保留",
            should_response=True,
        )
        stage_started = asyncio.Event()
        stage_cancelled = asyncio.Event()
        never_finishes = asyncio.Event()
        session.start_stage(
            response_id=response.response_id,
            generation=response.generation,
            stage="manual",
            operation=_blocked_until_cancelled(
                started=stage_started,
                cancelled=stage_cancelled,
                blocker=never_finishes,
            ),
        )
        await stage_started.wait()

        await session.end()

        assert session.response(response.response_id).state is module.ResponseState.CANCELLED
        assert session.active_response is None
        assert session.pending_utterances == ()
        assert session.utterance(UTTERANCE_2).state is module.UtteranceState.DISCARDED
        assert session.utterance(UTTERANCE_2).discard_reason == "session_ended"
        assert stage_cancelled.is_set() is True
        assert never_finishes.is_set() is False
        assert session.running_stage_count == 0
        assert [event_field(event, "type") for event in _terminal_events(delivery)] == [
            "response_cancelled"
        ]
        assert len(persistence.outcomes) == 1

    _run(exercise)


def test_same_session_accepts_a_new_response_after_reconnect() -> None:
    async def exercise() -> None:
        module, session, delivery, persistence, _observation = _session()
        old_response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="切断前",
            should_response=True,
        )
        old_generation = old_response.generation
        await session.disconnect()
        await session.reconnect()

        late_accepted = await session.accept_text_delta(
            response_id=old_response.response_id,
            generation=old_generation,
            text_sequence=1,
            text="古い結果",
            text_range=(0, 4),
        )
        new_response = await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="再接続後",
            should_response=True,
        )

        assert late_accepted is False
        assert new_response.response_id == RESPONSE_2
        assert new_response.state is module.ResponseState.IN_PROGRESS
        assert session.response(old_response.response_id).state is module.ResponseState.CANCELLED
        assert len(_response_started_events(delivery)) == 2
        assert len(persistence.outcomes) == 1

    _run(exercise)


def test_stage_observations_have_one_terminal_outcome_and_no_payload() -> None:
    async def exercise() -> None:
        _module, session, _delivery, _persistence, observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="秘密の本文",
            should_response=True,
        )
        await session.stage_started(
            response_id=response.response_id,
            generation=response.generation,
            stage="manual",
        )
        await session.stage_cancelled(
            response_id=response.response_id,
            generation=response.generation,
            stage="manual",
        )

        fields = [
            event_fields(item)
            for item in observation.observations
            if event_field(item, "stage") == "manual"
        ]
        assert [item["outcome"] for item in fields] == ["started", "cancelled"]
        assert all(item["stage"] == "manual" for item in fields)
        forbidden = {"transcript", "text", "assistant_text", "audio", "audio_bytes", "payload"}
        assert all(forbidden.isdisjoint(item) for item in fields)
        assert all("秘密の本文" not in repr(item) for item in fields)

    _run(exercise)


@pytest.mark.parametrize(
    ("terminal_method", "expected_outcome"),
    [
        ("stage_completed", "completed"),
        ("stage_failed", "failed"),
    ],
)
def test_completed_and_failed_stage_observations_have_no_payload(
    terminal_method: str,
    expected_outcome: str,
) -> None:
    async def exercise() -> None:
        _module, session, _delivery, _persistence, observation = _session()
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="記録してはいけない本文",
            should_response=True,
        )
        await session.stage_started(
            response_id=response.response_id,
            generation=response.generation,
            stage="manual",
        )

        await getattr(session, terminal_method)(
            response_id=response.response_id,
            generation=response.generation,
            stage="manual",
        )

        fields = [
            event_fields(item)
            for item in observation.observations
            if event_field(item, "stage") == "manual"
        ]
        assert [item["outcome"] for item in fields] == ["started", expected_outcome]
        assert all(item["stage"] == "manual" for item in fields)
        forbidden = {"transcript", "text", "assistant_text", "audio", "audio_bytes", "payload"}
        assert all(forbidden.isdisjoint(item) for item in fields)
        assert all("記録してはいけない本文" not in repr(item) for item in fields)

    _run(exercise)


def test_privacy_skipped_start_does_not_begin_generation_or_publish_payload() -> None:
    async def exercise() -> None:
        module = _core_module()
        delivery = RecordingDelivery()
        persistence = RecordingPersistence(content_skipped=True)
        observation = RecordingObservation()
        llm = RecordingLlm(deltas=(module.TextDelta(1, "漏れてはいけない", (0, 8)),))
        tts = RecordingTts()
        session = module.ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(RESPONSE_1),
            delivery=delivery,
            persistence=persistence,
            observation=observation,
            stt=RecordingStt(),
            llm=llm,
            tts=tts,
        )

        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="保存しない利用者発話",
            should_response=True,
        )
        await _wait_until(lambda: session.running_stage_count == 0)

        assert session.response(response.response_id).state is module.ResponseState.PRIVACY_SKIPPED
        assert llm.calls == []
        assert tts.calls == []
        assert [event_field(event, "type") for event in delivery.events] == [
            "response_privacy_skipped"
        ]
        assert len(persistence.outcomes) == 1
        assert event_field(persistence.outcomes[0], "state") is module.ResponseState.PRIVACY_SKIPPED

    _run(exercise)


def test_stt_failure_is_observed_without_starting_response_side_effects() -> None:
    async def exercise() -> None:
        error = RuntimeError("stt failure sentinel")
        stt = FailingStt(error)
        module, session, delivery, persistence, observation = _automatic_session(
            stt=stt,
            llm=RecordingLlm(deltas=()),
            tts=RecordingTts(),
        )

        task = session.start_transcription(
            utterance_id=UTTERANCE_1,
            audio=b"invalid upstream audio",
            should_response=True,
        )
        with pytest.raises(RuntimeError, match="stt failure sentinel"):
            await task

        assert stt.calls == [b"invalid upstream audio"]
        assert [event_field(item, "stage") for item in observation.observations] == [
            "stt",
            "stt",
        ]
        assert [event_field(item, "outcome") for item in observation.observations] == [
            "started",
            "failed",
        ]
        assert session.active_response is None
        assert persistence.starts == []
        assert persistence.outcomes == []
        assert delivery.events == []

    _run(exercise)


def test_delivery_failure_is_not_recorded_as_an_llm_failure() -> None:
    async def exercise() -> None:
        module = _core_module()

        @dataclass
        class FailingDeltaDelivery:
            events: list[object] = field(default_factory=list)

            async def publish(self, event: object) -> None:
                if event_field(event, "type") == "response_delta":
                    raise RuntimeError("delivery failure sentinel")
                self.events.append(event)

        delivery = FailingDeltaDelivery()
        persistence = RecordingPersistence()
        observation = RecordingObservation()
        session = module.ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(RESPONSE_1, RESPONSE_2),
            delivery=delivery,
            persistence=persistence,
            observation=observation,
            stt=RecordingStt(),
            llm=RecordingLlm(deltas=(module.TextDelta(1, "応答", (0, 2)),)),
            tts=RecordingTts(),
        )

        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="送信失敗",
            should_response=True,
        )
        await _wait_until(lambda: session.running_stage_count == 0)
        next_response = await session.finalize_utterance(
            utterance_id=UTTERANCE_2,
            transcript="送信失敗後の発話",
            should_response=True,
        )

        assert session.response(response.response_id).state is module.ResponseState.FAILED
        assert next_response.response_id == RESPONSE_2
        assert next_response.state is module.ResponseState.IN_PROGRESS
        llm_outcomes = [
            event_field(item, "outcome")
            for item in observation.observations
            if event_field(item, "stage") == "llm"
        ]
        assert llm_outcomes == ["started", "cancelled"]
        delivery_outcomes = [
            event_field(item, "outcome")
            for item in observation.observations
            if event_field(item, "stage") == "delivery"
        ]
        assert delivery_outcomes[:6] == [
            "started", "completed", "started", "failed", "started", "completed"
        ]
        await session.end()

    _run(exercise)


def test_cancel_finishes_while_delta_delivery_is_blocked() -> None:
    async def exercise() -> None:
        module = _core_module()
        delivery = BlockingDelivery("response_delta")
        persistence = RecordingPersistence()
        session = module.ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(RESPONSE_1),
            delivery=delivery,
            persistence=persistence,
            observation=RecordingObservation(),
            stt=RecordingStt(),
            llm=RecordingLlm(deltas=(module.TextDelta(1, "応答", (0, 2)),)),
            tts=RecordingTts(),
        )
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="中断",
            should_response=True,
        )
        await delivery.started.wait()

        cancelled = await asyncio.wait_for(
            session.cancel_response(response_id=response.response_id, reason="barge_in"),
            timeout=0.1,
        )
        await delivery.cancelled.wait()
        await _wait_until(lambda: session.running_stage_count == 0)

        assert cancelled.state is module.ResponseState.CANCELLED
        assert [event_field(event, "type") for event in delivery.events] == [
            "response_started",
            "response_cancelled",
        ]
        assert len(persistence.outcomes) == 1

    _run(exercise)


def test_cancel_finishes_while_terminal_persistence_is_blocked() -> None:
    async def exercise() -> None:
        module = _core_module()
        persistence = BlockingTerminalPersistence()
        session = module.ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(RESPONSE_1),
            delivery=RecordingDelivery(),
            persistence=persistence,
            observation=RecordingObservation(),
            stt=RecordingStt(),
            llm=BlockingLlm(),
            tts=RecordingTts(),
        )
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1,
            transcript="永続化待機",
            should_response=True,
        )

        cancelled = await asyncio.wait_for(
            session.cancel_response(response_id=response.response_id, reason="barge_in"),
            timeout=0.1,
        )
        await persistence.persist_started.wait()
        assert cancelled.state is module.ResponseState.CANCELLED
        assert session.response(response.response_id).state is module.ResponseState.CANCELLED

        persistence.release_persist.set()
        await _wait_until(lambda: session.running_stage_count == 0)
        assert len(persistence.outcomes) == 1

    _run(exercise)
