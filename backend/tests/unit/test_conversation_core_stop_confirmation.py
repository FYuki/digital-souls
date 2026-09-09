"""取消成立にはproviderの終了と出力停止の双方が必要。"""
import asyncio

import pytest

from app.conversation_core import (
    AudioSegment, ConversationCoreSession, ResponseState, ResponseStopResult, TextDelta,
)
from app.conversation_core.provider_result_audit import PROVIDER_RESULT_METRICS
from tests.conversation_core_test_support import (
    BlockingLlm, RecordingDelivery, RecordingObservation, RecordingPersistence,
    RecordingStt, RecordingTts, response_id_factory,
)


class HeldStop:
    def __init__(self, result=0):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.result = result
        self.closed = asyncio.Event()

    async def stop_response(self, response):
        self.calls.append(response)
        self.entered.set()
        try:
            await self.release.wait()
            if isinstance(self.result, Exception):
                raise self.result
            return ResponseStopResult(self.result)
        finally:
            self.closed.set()


class LateLlm:
    def __init__(self):
        self.entered = asyncio.Event()
        self.late = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def generate(self, _):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            yield TextDelta(1, "後😀", (0, 2))
            self.late.set()
            await self.release.wait()
        finally:
            self.closed.set()


def make_session(stop, llm=None, delivery=None, **kwargs):
    delivery = delivery or RecordingDelivery()
    persistence, observation = RecordingPersistence(), RecordingObservation()
    session = ConversationCoreSession(
        session_id="s", response_id_factory=response_id_factory("r", "next"),
        delivery=delivery, persistence=persistence, observation=observation,
        stt=RecordingStt(), llm=llm or BlockingLlm(), tts=RecordingTts(),
        cancellation=stop, **kwargs,
    )
    return session, delivery, persistence, observation


async def start(session):
    return await session.finalize_utterance(
        utterance_id="u", transcript="始めて", should_response=True,
    )


async def tick():
    for _ in range(15):
        await asyncio.sleep(0)


@pytest.mark.parametrize("output_first", [True, False])
def test_both_stops_required_and_late_receipt_is_reported_separately(output_first):
    async def exercise():
        stop, llm = HeldStop(), LateLlm()
        session, delivery, persistence, observation = make_session(stop, llm)
        response = await start(session)
        await llm.entered.wait()
        cancelling = asyncio.create_task(session.cancel_response(response_id="r", reason="barge_in"))
        await asyncio.wait_for(asyncio.gather(stop.entered.wait(), llm.late.wait()), 0.5)
        assert session.active_response.state is ResponseState.CANCELLING
        assert not session.active_response.state.is_terminal
        assert not await session.accept_text_delta(
            response_id="r", generation=response.generation, text_sequence=2,
            text="遅延", text_range=(2, 4),
        )
        assert not await session.accept_audio_segment(
            response_id="r", generation=response.generation, audio_sequence=1,
            audio=b"late", text_range=(0, 2),
        )
        assert (await session.complete_response(response_id="r", generation=response.generation)).state is ResponseState.CANCELLING
        if output_first:
            stop.release.set()
        else:
            llm.release.set()
        await tick()
        assert not cancelling.done()
        assert persistence.outcomes == []
        assert not [item for item in observation.observations if item.stage in PROVIDER_RESULT_METRICS]
        stop.release.set()
        llm.release.set()
        result = await asyncio.wait_for(cancelling, 0.5)
        await tick()
        assert result.state is ResponseState.CANCELLED
        assert llm.closed.is_set() and stop.closed.is_set()
        assert result.generated_text == "" and result.audio_segments == ()
        assert not [event for event in delivery.events if event.type == "response_delta"]
        stats = {item.stage: item.value for item in observation.observations}
        assert stats["provider_result_text_during_stop_utf16_units"] == 3
        assert stats["provider_result_text_after_cancel_utf16_units"] == 0
        assert stats["provider_result_observation_closed"] == 1
        assert len([item for item in observation.observations if item.stage in PROVIDER_RESULT_METRICS]) == 6
        assert len(persistence.outcomes) == 1
        await session.end()
        assert session.running_stage_count == 0
    asyncio.run(exercise())


def test_duplicate_requests_share_stop_and_cancelled_waiter_does_not_abort_it():
    async def exercise():
        stop = HeldStop()
        session, _, persistence, _ = make_session(stop)
        await start(session)
        first = asyncio.create_task(session.cancel_response(response_id="r", reason="barge_in"))
        await stop.entered.wait()
        second = asyncio.create_task(session.cancel_response(response_id="r", reason="duplicate"))
        await tick()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not stop.closed.is_set()
        assert len(stop.calls) == 1
        await session.finalize_utterance(utterance_id="u2", transcript="次", should_response=True)
        assert len(persistence.starts) == 1
        stop.release.set()
        result = await asyncio.wait_for(second, 0.5)
        await tick()
        assert result.terminal_reason == "barge_in"
        assert len(persistence.starts) == 2
        assert session.active_response.response_id == "next"
        await session.end()
        assert session.running_stage_count == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("invalid", [True, -1, 1, 0.5, None, RuntimeError("unconfirmed")])
def test_invalid_or_failed_output_confirmation_is_not_cancelled(invalid):
    async def exercise():
        stop = HeldStop(invalid)
        session, delivery, persistence, _ = make_session(stop)
        await start(session)
        stop.release.set()
        result = await session.cancel_response(response_id="r", reason="barge_in")
        await tick()
        assert result.state is ResponseState.FAILED
        assert result.terminal_reason == "output_stop_unconfirmed"
        assert not [event for event in delivery.events if event.type == "response_cancelled"]
        assert persistence.outcomes[0].terminal_state_bounds_ns is None
        await session.end()
    asyncio.run(exercise())


def test_timeout_cleans_waiter_and_is_not_success():
    async def exercise():
        stop = HeldStop()
        session, _, _, _ = make_session(stop, cancellation_timeout=0.01)
        await start(session)
        result = await asyncio.wait_for(session.cancel_response(response_id="r", reason="barge_in"), 0.5)
        assert result.state is ResponseState.FAILED
        assert stop.closed.is_set()
        await session.end()
        assert session.running_stage_count == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("terminal", ["disconnect", "end", "failure", "privacy"])
def test_terminal_race_waits_for_provider_cleanup_without_claiming_stop(terminal):
    async def exercise():
        stop, llm = HeldStop(), LateLlm()
        session, _, persistence, _ = make_session(stop, llm)
        response = await start(session)
        await llm.entered.wait()
        cancelling = asyncio.create_task(session.cancel_response(response_id="r", reason="barge_in"))
        await asyncio.wait_for(asyncio.gather(stop.entered.wait(), llm.late.wait()), 0.5)
        if terminal == "failure":
            ending = asyncio.create_task(session.fail_response(response_id="r", generation=response.generation))
        elif terminal == "privacy":
            ending = asyncio.create_task(session.privacy_skip_response(response_id="r", generation=response.generation))
        else:
            ending = asyncio.create_task(getattr(session, terminal)())
        await tick()
        assert session.response("r").state is ResponseState.CANCELLING
        assert persistence.outcomes == []
        assert stop.closed.is_set()
        llm.release.set()
        result, _ = await asyncio.wait_for(asyncio.gather(cancelling, ending), 0.5)
        assert result.state is (ResponseState.PRIVACY_SKIPPED if terminal == "privacy" else ResponseState.FAILED)
        assert llm.closed.is_set()
        await session.end()
        assert session.running_stage_count == 0
    asyncio.run(exercise())


def test_response_stage_can_request_cancellation_without_waiting_for_itself():
    async def exercise():
        stop = HeldStop()
        session, _, _, _ = make_session(stop)
        response = await start(session)
        async def operation():
            await session.cancel_response(response_id="r", reason="stage_request")
        stage = session.start_stage(response_id="r", generation=response.generation, stage="custom", operation=operation())
        await asyncio.wait_for(stop.entered.wait(), 0.5)
        stop.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stage, 0.5)
        async def finished():
            while not session.response("r").state.is_terminal:
                await asyncio.sleep(0)
        await asyncio.wait_for(finished(), 0.5)
        assert session.response("r").state is ResponseState.CANCELLED
        await session.end()
        assert session.running_stage_count == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("prefix", [0, 1, 2])
def test_confirmed_prefix_is_saved_without_regressing_newer_playback(prefix):
    async def exercise():
        stop = HeldStop(prefix)
        session, _, persistence, _ = make_session(stop)
        response = await start(session)
        await session.accept_text_delta(response_id="r", generation=response.generation,
            text_sequence=1, text="一二", text_range=(0, 2))
        for sequence in (1, 2):
            await session.accept_audio_segment(response_id="r", generation=response.generation,
                audio_sequence=sequence, audio=b"pcm", text_range=(sequence - 1, sequence))
        cancelling = asyncio.create_task(session.cancel_response(response_id="r", reason="barge_in"))
        await stop.entered.wait()
        await session.confirm_playback(response_id="r", last_played_audio_sequence=1)
        stop.release.set()
        result = await cancelling
        await tick()
        assert result.last_played_audio_sequence == max(1, prefix)
        assert persistence.outcomes[0].last_played_audio_sequence == max(1, prefix)
        await session.end()
    asyncio.run(exercise())


def test_output_ack_does_not_skip_inflight_delivery_cleanup():
    async def exercise():
        class HeldDelivery(RecordingDelivery):
            def __init__(self):
                super().__init__()
                self.entered, self.cancelled, self.release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def publish(self, event):
                if event.type == "response_delta":
                    self.entered.set()
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        self.cancelled.set()
                        await self.release.wait()
                await super().publish(event)
        stop, delivery = HeldStop(), HeldDelivery()
        session, _, persistence, _ = make_session(stop, delivery=delivery)
        response = await start(session)
        sending = asyncio.create_task(session.accept_text_delta(response_id="r",
            generation=response.generation, text_sequence=1, text="配送", text_range=(0, 2)))
        await delivery.entered.wait()
        stop.release.set()
        cancelling = asyncio.create_task(session.cancel_response(response_id="r", reason="barge_in"))
        await delivery.cancelled.wait()
        await tick()
        assert not cancelling.done()
        assert persistence.outcomes == []
        delivery.release.set()
        await asyncio.wait_for(asyncio.gather(sending, cancelling), 0.5)
        await tick()
        types = [event.type for event in delivery.events]
        assert types.index("response_delta") < types.index("response_cancelled")
        await session.end()
    asyncio.run(exercise())


def test_tts_receipt_during_stop_is_not_delivered_or_hidden():
    async def exercise():
        class Llm:
            async def generate(self, _):
                yield TextDelta(1, "開始。", (0, 3))
                await asyncio.Event().wait()
        class Tts:
            def __init__(self):
                self.entered, self.late, self.release, self.closed = [asyncio.Event() for _ in range(4)]
            async def synthesize(self, _):
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    yield AudioSegment(1, b"late-pcm", (0, 3))
                    self.late.set()
                    await self.release.wait()
                finally:
                    self.closed.set()
        stop, tts = HeldStop(), Tts()
        observation, delivery = RecordingObservation(), RecordingDelivery()
        def clock():
            assert tts.closed.is_set() and stop.closed.is_set()
            return 100
        session = ConversationCoreSession(session_id="s", response_id_factory=lambda: "r",
            delivery=delivery, persistence=RecordingPersistence(), observation=observation,
            stt=RecordingStt(), llm=Llm(), tts=tts, cancellation=stop, monotonic_ns=clock)
        await start(session)
        await tts.entered.wait()
        stop.release.set()
        cancelling = asyncio.create_task(session.cancel_response(response_id="r", reason="barge_in"))
        await tts.late.wait()
        await tick()
        assert not cancelling.done()
        tts.release.set()
        await asyncio.wait_for(cancelling, 0.5)
        assert not [event for event in delivery.events if event.type == "response_audio_segment"]
        stats = {item.stage: item.value for item in observation.observations}
        assert stats["provider_result_audio_during_stop_bytes"] == 8
        assert stats["provider_result_audio_after_cancel_bytes"] == 0
        await session.end()
    asyncio.run(exercise())


def test_unknown_or_terminal_response_does_not_request_output_stop():
    async def exercise():
        stop = HeldStop()
        session, _, _, _ = make_session(stop)
        assert await session.cancel_response(response_id="unknown", reason="barge_in") is None
        response = await start(session)
        await session.complete_response(response_id="r", generation=response.generation)
        assert (await session.cancel_response(response_id="r", reason="barge_in")).state is ResponseState.COMPLETED
        assert stop.calls == []
        await session.end()
    asyncio.run(exercise())


def test_cancellation_during_start_delivery_does_not_launch_or_invent_provider_audit():
    async def exercise():
        class HeldStartDelivery(RecordingDelivery):
            def __init__(self):
                super().__init__()
                self.entered, self.cancelled, self.release = [asyncio.Event() for _ in range(3)]
            async def publish(self, event):
                if event.type == "response_started":
                    self.entered.set()
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        self.cancelled.set()
                        await self.release.wait()
                await super().publish(event)
        stop, llm, delivery = HeldStop(), BlockingLlm(), HeldStartDelivery()
        session, _, persistence, observation = make_session(stop, llm, delivery)
        starting = asyncio.create_task(start(session))
        await delivery.entered.wait()
        stop.release.set()
        cancelling = asyncio.create_task(session.cancel_response(response_id="r", reason="barge_in"))
        await delivery.cancelled.wait()
        await tick()
        assert session.response("r").state is ResponseState.CANCELLING
        assert persistence.outcomes == []
        delivery.release.set()
        await asyncio.wait_for(asyncio.gather(starting, cancelling), 0.5)
        assert llm.calls == []
        assert not [item for item in observation.observations if item.stage in PROVIDER_RESULT_METRICS]
        await session.end()
        assert session.running_stage_count == 0
    asyncio.run(exercise())
