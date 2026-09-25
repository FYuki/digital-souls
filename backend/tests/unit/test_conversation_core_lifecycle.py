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

from tests.conversation_core_lifecycle_support import (
    SESSION_ID,
    RESPONSE_1,
    RESPONSE_2,
    UTTERANCE_1,
    UTTERANCE_2,
    UTTERANCE_3,
    _core_module,
    _session,
    _automatic_session,
    _run,
    _wait_until,
    _response_started_events,
    _terminal_events,
    _blocked_until_cancelled,
    CancellationSuppressingLlm,
    FirstCallFailingLlm,
    FirstCallFailingTts,
    FirstCallFailingPersistence,
    FirstTerminalFailingPersistence,
    BlockingStt,
    CancellationSuppressingTts,
)

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


def test_response_start_failure_terminates_once_and_allows_the_next_response(caplog) -> None:
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
    assert "Core task failed: type=RuntimeError" in caplog.text
    assert "start_response" in caplog.text
    assert "response start failure sentinel" not in caplog.text
    assert "開始に失敗する入力" not in caplog.text


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
