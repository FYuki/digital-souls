from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from app.livekit_transport.delivery import CoreEventDelivery
from app.livekit_transport.production import _ConversationCoreBridge
from app.livekit_transport.text_input import TextInputReceiver


def test_runtime_bridge_delivers_text_and_lookup_without_stt_or_duplicate_submission() -> None:
    async def run() -> None:
        session_id, participant_id, response_id = (str(uuid4()) for _ in range(3))
        results: list[dict[str, object]] = []
        tasks: list[asyncio.Task[None]] = []
        submit = AsyncMock(return_value=response_id)
        transcribe = AsyncMock()

        async def publish(result: dict[str, object]) -> None:
            results.append(result)

        receiver = TextInputReceiver(
            session_id=session_id, participant_id=participant_id,
            submit=submit, publish=publish, accepting_input=lambda: True,
        )
        bridge = _ConversationCoreBridge(
            SimpleNamespace(start_transcription=transcribe),
            lambda operation: tasks.append(asyncio.create_task(operation)),
            text_input=receiver,
        )
        delivery = CoreEventDelivery(core_port=bridge)
        event = {
            "protocol_version": "1.1", "type": "user_text_submitted",
            "event_id": str(uuid4()), "session_id": session_id,
            "speaker": {"role": "user", "participant_id": participant_id},
            "monotonic_timestamp_ms": 1, "text": "同じ会話へのテキスト入力",
        }
        payload = json.dumps(event).encode()
        delivery.receive(payload)
        delivery.receive(payload)
        await asyncio.gather(*tasks)
        submit.assert_awaited_once_with(event["event_id"], event["text"])
        transcribe.assert_not_called()
        assert results[-1]["status"] == "accepted"
        query = {**event, "type": "user_input_result_requested",
                 "event_id": str(uuid4()), "input_event_id": event["event_id"]}
        del query["text"]
        delivery.receive(json.dumps(query).encode())
        await asyncio.gather(*tasks)
        assert results[-1]["input_event_id"] == event["event_id"]
        assert results[-1]["response_id"] == response_id
        assert submit.await_count == 1
    asyncio.run(run())
