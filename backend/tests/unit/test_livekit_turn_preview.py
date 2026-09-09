import asyncio
import json

import pytest

import app.livekit_transport.production as production


def start(bridge):
    bridge.notify(
        json.dumps(
            {
                "type": "speech_started",
                "speaker": {"role": "user"},
                "utterance_id": "retry",
                "response_id": "old",
            }
        ).encode()
    )


@pytest.mark.parametrize("initial", ["backchannel", "indeterminate"])
def test_additional_audio_can_change_preview_without_waiting_for_speech_end(initial):
    async def exercise():
        requests, tasks = [], []

        class Core:
            accepting_input = True

            async def preview_turn(self, **request):
                requests.append(request)
                return initial if len(requests) == 1 else "take_turn"

        bridge = production._ConversationCoreBridge(
            Core(), lambda op: tasks.append(asyncio.create_task(op))
        )
        start(bridge)
        window = b"\x00\x10" * (production.STT_TURN_PREVIEW_PCM_BYTES // 2)
        bridge.receive_microphone(window)
        await asyncio.gather(*tasks)
        assert len(requests) == 1
        bridge.receive_microphone(window[:6400])
        await asyncio.gather(*tasks)
        assert len(requests) == 1
        bridge.receive_microphone(window[6400:])
        await asyncio.gather(*tasks)
        assert [r["audio"] for r in requests] == [window, window * 2]
        bridge.receive_microphone(window)
        await asyncio.gather(*tasks)
        assert len(requests) == 2

    asyncio.run(exercise())


def test_preview_retries_are_bounded_even_when_no_decision_is_possible():
    async def exercise():
        requests, tasks = [], []

        class Core:
            accepting_input = True

            async def preview_turn(self, **request):
                requests.append(request)
                return "indeterminate"

        bridge = production._ConversationCoreBridge(
            Core(), lambda op: tasks.append(asyncio.create_task(op))
        )
        start(bridge)
        for _ in range(10):
            bridge.receive_microphone(
                b"\x00\x10" * (production.STT_TURN_PREVIEW_PCM_BYTES // 2)
            )
            await asyncio.gather(*tasks)
        assert len(requests) == 3

    asyncio.run(exercise())


@pytest.mark.parametrize("closed", [False, True])
def test_inflight_preview_serializes_new_audio_and_does_not_restart_closed_capture(
    closed,
):
    async def exercise():
        requests, tasks = [], []
        entered, release = asyncio.Event(), asyncio.Event()

        class Core:
            accepting_input = True

            async def preview_turn(self, **request):
                requests.append(request)
                entered.set()
                await release.wait()
                return "backchannel"

        bridge = production._ConversationCoreBridge(
            Core(), lambda op: tasks.append(asyncio.create_task(op))
        )
        start(bridge)
        window = b"\x00\x10" * (production.STT_TURN_PREVIEW_PCM_BYTES // 2)
        bridge.receive_microphone(window)
        await entered.wait()
        bridge.receive_microphone(window)
        assert len(requests) == 1
        if closed:
            bridge._user_audio_captures[0].finalized = True
        release.set()
        while any(not t.done() for t in tasks):
            await asyncio.gather(*tasks)
        assert len(requests) == (1 if closed else 2)

    asyncio.run(exercise())


def test_cancelled_preview_does_not_resurrect_queued_audio():
    async def exercise():
        tasks = []
        entered = asyncio.Event()

        class Core:
            accepting_input = True

            async def preview_turn(self, **request):
                entered.set()
                await asyncio.Future()

        bridge = production._ConversationCoreBridge(
            Core(), lambda op: tasks.append(asyncio.create_task(op))
        )
        start(bridge)
        window = b"\x00\x10" * (production.STT_TURN_PREVIEW_PCM_BYTES // 2)
        bridge.receive_microphone(window)
        await entered.wait()
        bridge.receive_microphone(window)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        try:
            assert len(tasks) == 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())


def test_queued_final_transcription_runs_before_an_additional_preview():
    async def exercise():
        tasks, calls = [], []
        entered, release, finish_final = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Core:
            accepting_input = True

            async def preview_turn(self, **request):
                calls.append("preview")
                entered.set()
                await release.wait()
                return "backchannel"

            def start_transcription(self, **request):
                calls.append("final")
                return asyncio.create_task(finish_final.wait())

        bridge = production._ConversationCoreBridge(Core(), lambda op: tasks.append(asyncio.create_task(op)))
        start(bridge)
        window = b"\x00\x10" * (production.STT_TURN_PREVIEW_PCM_BYTES // 2)
        bridge.receive_microphone(window)
        await entered.wait()
        bridge.receive_microphone(window)
        await bridge._enqueue_user_audio(utterance_id="final", microphone_pcm=window, interrupted_response_id="old")
        release.set()
        await asyncio.gather(*tasks)
        assert calls == ["preview", "final"]
        finish_final.set()
        await asyncio.sleep(0)

    asyncio.run(exercise())


def test_no_new_preview_after_input_stops_accepting_audio():
    async def exercise():
        tasks, calls = [], []

        class Core:
            accepting_input = True

            async def preview_turn(self, **request):
                calls.append(request)
                return "backchannel"

        core = Core()
        bridge = production._ConversationCoreBridge(core, lambda op: tasks.append(asyncio.create_task(op)))
        start(bridge)
        window = b"\x00\x10" * (production.STT_TURN_PREVIEW_PCM_BYTES // 2)
        bridge.receive_microphone(window)
        await asyncio.gather(*tasks)
        core.accepting_input = False
        bridge.receive_microphone(window)
        await asyncio.gather(*tasks)
        assert len(calls) == 1

    asyncio.run(exercise())
