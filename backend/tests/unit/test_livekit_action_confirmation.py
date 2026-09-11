"""LiveKitの画面入力は予約済み会話へ限定し、既存音声入力へ混ぜない。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.livekit_transport.production import ProductionRuntimeManager


def test_livekit_confirmation_requires_matching_live_binding_and_waits_for_question():
    async def run():
        calls = []
        core = SimpleNamespace(active_response=object(), accepting_input=True)

        async def finalize(**kwargs):
            calls.append(kwargs)
            return object()

        core.finalize_utterance = finalize
        manager = object.__new__(ProductionRuntimeManager)
        manager._core_sessions = {"voice": core}
        reservation = SimpleNamespace(
            request={"character_id": "miori", "conversation_id": "conversation"}
        )
        manager._sessions = SimpleNamespace(
            get=lambda key: reservation if key == "voice" else None
        )
        assert manager.confirmation_session("miori", "conversation") == "voice"
        assert manager.confirmation_session("other", "conversation") is None
        assert manager.confirmation_session("miori", "other") is None
        with pytest.raises(ValueError):
            await manager.submit_action_confirmation(
                "voice", "other", "conversation", "id", "承認", lambda: True
            )
        still_waiting = lambda: True
        pending = asyncio.create_task(
            manager.submit_action_confirmation(
                "voice",
                "miori",
                "conversation",
                "id",
                "画面で承認",
                still_waiting,
            )
        )
        await asyncio.sleep(0)
        assert not calls
        core.active_response = None
        assert await pending
        assert calls == [
            {
                "utterance_id": "id",
                "transcript": "画面で承認",
                "should_response": True,
                "control_request_id": "id",
                "control_input_valid": still_waiting,
            }
        ]
        assert not await manager.submit_action_confirmation(
            "voice", "miori", "conversation", "expired", "承認", lambda: False
        )
        assert len(calls) == 1
        core.accepting_input = False
        for active in (None, object()):
            core.active_response = active
            assert not await manager.submit_action_confirmation(
                "voice", "miori", "conversation", "closed-input", "承認", lambda: True
            )
        assert len(calls) == 1

    asyncio.run(run())


def test_confirmation_expiring_while_core_lock_is_held_does_not_register_input():
    from app.conversation_core import ConversationCoreSession, TextDelta
    from tests.conversation_core_test_support import (
        RecordingDelivery, RecordingObservation, RecordingPersistence,
        RecordingStt, RecordingTts, response_id_factory,
    )

    async def run():
        seen = []

        class Llm:
            async def generate(self, text):
                seen.append(text)
                yield TextDelta(1, "結果です。", (0, 5))

        core = ConversationCoreSession(
            session_id="voice", response_id_factory=response_id_factory("r1"),
            delivery=RecordingDelivery(), persistence=RecordingPersistence(),
            observation=RecordingObservation(), stt=RecordingStt(),
            llm=Llm(), tts=RecordingTts(),
        )
        manager = object.__new__(ProductionRuntimeManager)
        manager._core_sessions = {"voice": core}
        manager._sessions = {"voice": SimpleNamespace(request={
            "character_id": "miori", "conversation_id": "conversation",
        })}
        valid, checked = [True], asyncio.Event()

        def waiting():
            checked.set()
            return valid[0]

        try:
            async with core._state_lock:
                submitted = asyncio.create_task(manager.submit_action_confirmation(
                    "voice", "miori", "conversation", "expired", "画面で承認", waiting,
                ))
                await asyncio.wait_for(checked.wait(), 1)
                assert not submitted.done()
                valid[0] = False
            assert not await asyncio.wait_for(submitted, 1)
            assert core.active_response is None and not core.pending_utterances
            with pytest.raises(KeyError):
                core.utterance("expired")
            assert not seen
        finally:
            await core.end()

    asyncio.run(run())
