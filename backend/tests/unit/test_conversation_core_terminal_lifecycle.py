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


@pytest.mark.parametrize("phase", ["preview", "final"])
@pytest.mark.parametrize("already_completed", [False, True])
def test_turn_cancel_observation_requires_a_cancelled_response(phase, already_completed) -> None:
    async def exercise() -> None:
        module, session, delivery, _persistence, observation = _session()
        first = await session.finalize_utterance(
            utterance_id=UTTERANCE_1, transcript="応答", should_response=True,
        )
        try:
            if already_completed:
                await session.complete_response(response_id=first.response_id, generation=first.generation)
            if phase == "preview":
                await session.preview_turn(utterance_id=UTTERANCE_2, audio=b"\0\0", interrupted_response_id=first.response_id)
            else:
                await session.start_transcription(utterance_id=UTTERANCE_2, audio=b"\0\0", should_response=True, interrupted_response_id=first.response_id)
            expected = 0 if already_completed else 1
            assert sum(item.stage == "server_cancelled" and item.utterance_id == UTTERANCE_2 for item in observation.observations) == expected
            assert sum(event.type == "response_cancelled" and event.response_id == first.response_id for event in delivery.events) == expected
            assert session.response(first.response_id).state is (module.ResponseState.COMPLETED if already_completed else module.ResponseState.CANCELLED)
        finally:
            await session.end()
    _run(exercise)


def test_cancel_clock_brackets_state_transition_and_survives_delayed_delivery() -> None:
    async def exercise() -> None:
        module, session, delivery, _, _ = _session()
        persistence = BlockingTerminalPersistence()
        session._persistence = persistence
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1, transcript="中断対象", should_response=True,
        )
        states = []
        ticks = iter((123_456_001, 123_456_999))
        def clock() -> int:
            states.append(session.response(response.response_id).state)
            return next(ticks)
        session._monotonic_ns = clock
        await session.cancel_response(response_id=response.response_id, reason="barge_in")
        await session.cancel_response(response_id=response.response_id, reason="barge_in")
        await persistence.persist_started.wait()
        assert states == [module.ResponseState.IN_PROGRESS, module.ResponseState.CANCELLED]
        assert _terminal_events(delivery) == []
        persistence.release_persist.set()
        await _wait_until(lambda: len(_terminal_events(delivery)) == 1)
        assert event_field(_terminal_events(delivery)[0], "terminal_state_bounds_ns") == (123_456_001, 123_456_999)
        assert event_field(persistence.outcomes[0], "terminal_state_bounds_ns") == (123_456_001, 123_456_999)
        await session.end()
    _run(exercise)
