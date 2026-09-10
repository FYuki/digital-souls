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

        core.finalize_utterance = finalize
        manager = object.__new__(ProductionRuntimeManager)
        manager._core_sessions = {"voice": core}
        reservation = SimpleNamespace(
            request={"character_id": "miori", "conversation_id": "conversation"}
        )
        manager._sessions = SimpleNamespace(
            get=lambda key: reservation if key == "voice" else None
        )
        with pytest.raises(ValueError):
            await manager.submit_action_confirmation(
                "voice", "other", "conversation", "id", "承認", lambda: True
            )
        pending = asyncio.create_task(
            manager.submit_action_confirmation(
                "voice",
                "miori",
                "conversation",
                "id",
                "画面で承認",
                lambda: True,
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
            }
        ]
        assert not await manager.submit_action_confirmation(
            "voice", "miori", "conversation", "expired", "承認", lambda: False
        )
        assert len(calls) == 1

    asyncio.run(run())
