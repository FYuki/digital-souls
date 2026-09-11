from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.livekit_transport.production import _ConversationCoreBridge


def test_focus_gate_drops_open_capture_and_muted_media_without_stopping_response() -> None:
    async def exercise() -> None:
        transcribed: list[dict[str, object]] = []
        tasks: list[asyncio.Task[None]] = []

        def transcribe(**request: object) -> asyncio.Task[None]:
            async def record() -> None:
                transcribed.append(request)
            return asyncio.create_task(record())

        core = SimpleNamespace(
            start_transcription=transcribe, discard_utterance=AsyncMock(),
            cancel_response=AsyncMock(), end=AsyncMock(), accepting_input=True,
        )
        text = SimpleNamespace(receive=AsyncMock())
        bridge = _ConversationCoreBridge(
            core, lambda operation: tasks.append(asyncio.create_task(operation)),
            media_tail_seconds=0, text_input=text,
        )

        def notify(kind: str, **fields: object) -> None:
            bridge.notify(json.dumps({"type": kind, "speaker": {"role": "user"}, **fields}).encode())

        notify("speech_started", utterance_id="unfinished")
        bridge.receive_microphone(b"\x20\x00" * 480)
        notify("audio_input_suppression_changed", suppressed=True, reason="text_focus")
        bridge.receive_microphone(b"\x55\x00" * 480)
        notify("speech_started", utterance_id="muted-speech")
        notify("speech_stopped", utterance_id="muted-speech")
        notify("user_text_submitted", text="テキストは送信できる")
        await asyncio.gather(*tasks)
        core.discard_utterance.assert_awaited_once_with(utterance_id="unfinished", reason="input_suppressed")
        assert not transcribed
        text.receive.assert_awaited_once()
        core.cancel_response.assert_not_awaited()
        core.end.assert_not_awaited()

        notify("audio_input_suppression_changed", suppressed=False, reason="text_focus")
        notify("speech_started", utterance_id="new-speech")
        bridge.receive_microphone(b"\x33\x00" * 480)
        notify("speech_stopped", utterance_id="new-speech")
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        assert len(transcribed) == 1
        assert transcribed[0]["utterance_id"] == "new-speech"
        assert transcribed[0]["audio"] == b"\x33\x00" * 480

    asyncio.run(exercise())

def test_focus_and_manual_mute_are_independent_backend_gates() -> None:
    async def exercise() -> None:
        core = SimpleNamespace(discard_utterance=AsyncMock())
        tasks: list[asyncio.Task[None]] = []
        bridge = _ConversationCoreBridge(core, lambda operation: tasks.append(asyncio.create_task(operation)))

        def notify(kind: str, **fields: object) -> None:
            bridge.notify(json.dumps({"type": kind, **fields}).encode())

        notify("session_muted")
        notify("audio_input_suppression_changed", suppressed=True, reason="text_focus")
        notify("audio_input_suppression_changed", suppressed=False, reason="text_focus")
        bridge.receive_microphone(b"\x55\x00" * 480)
        assert not bridge._microphone_preroll
        notify("audio_input_suppression_changed", suppressed=True, reason="text_focus")
        notify("session_resumed")
        bridge.receive_microphone(b"\x55\x00" * 480)
        assert not bridge._microphone_preroll
        notify("audio_input_suppression_changed", suppressed=False, reason="text_focus")
        bridge.receive_microphone(b"\x33\x00" * 480)
        assert bridge._microphone_preroll == b"\x33\x00" * 480
        assert not tasks

    asyncio.run(exercise())
