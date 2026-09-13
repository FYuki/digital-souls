from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.livekit_transport.delivery import TerminalProtocolError, decode_core_event
from app.livekit_transport.text_input import TextInputReceiver, TextInputRejected
from app.voice_session.validation import parse_voice_session_event


SESSION = "10000000-0000-4000-8000-000000000001"
PARTICIPANT = "10000000-0000-4000-8000-000000000002"
RESPONSE = "10000000-0000-4000-8000-000000000003"


def event(kind: str = "user_text_submitted", **fields: object) -> dict[str, object]:
    return {
        "protocol_version": "1.1", "event_id": str(uuid4()), "session_id": SESSION,
        "type": kind, "monotonic_timestamp_ms": 1,
        "speaker": {"role": "user", "participant_id": PARTICIPANT},
        **({"text": "続きを教えて"} if kind == "user_text_submitted" else {}),
        **fields,
    }


class Harness:
    def __init__(self, **kwargs: object) -> None:
        self.submissions: list[tuple[str, str]] = []
        self.results: list[dict[str, object]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.error: Exception | None = None
        self.fail_publish = False
        self.receiver = TextInputReceiver(
            session_id=SESSION, participant_id=PARTICIPANT,
            submit=self.submit, publish=self.publish,
            accepting_input=lambda: True, **kwargs,
        )

    async def submit(self, input_id: str, text: str) -> str:
        self.submissions.append((input_id, text))
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return RESPONSE

    async def publish(self, result: dict[str, object]) -> None:
        # 出力も共有wire契約を通す。結果は本文を返さない。
        parse_voice_session_event(result)
        decode_core_event(json.dumps(result).encode())
        assert "text" not in result
        if self.fail_publish:
            raise OSError("simulated disconnect")
        self.results.append(result)


def test_concurrent_retry_and_query_do_not_repeat_pending_input() -> None:
    async def run() -> None:
        h = Harness()
        h.release.clear()
        request = event()
        first = asyncio.create_task(h.receiver.receive(request))
        await h.started.wait()
        await h.receiver.receive(dict(request))
        await h.receiver.receive(event("user_input_result_requested", input_event_id=request["event_id"]))
        assert [r["status"] for r in h.results] == ["processing", "processing"]
        assert len(h.submissions) == 1
        h.release.set()
        await first
        await h.receiver.receive(dict(request))
        assert [r["status"] for r in h.results[-2:]] == ["accepted", "accepted"]
        assert h.results[-1]["response_id"] == RESPONSE
        assert len(h.submissions) == 1
    asyncio.run(run())


def test_lookup_seals_missing_input_before_late_delivery() -> None:
    async def run() -> None:
        h = Harness()
        request = event()
        query = event("user_input_result_requested", input_event_id=request["event_id"])
        await h.receiver.receive(query)
        await h.receiver.receive(request)
        await h.receiver.receive(query)
        assert [r["status"] for r in h.results] == ["not_received"] * 3
        assert h.submissions == []
        # 明示再送の新IDだけを処理する。
        await h.receiver.receive(event(text=request["text"]))
        assert len(h.submissions) == 1
    asyncio.run(run())


def test_receipt_survives_lost_result_without_new_submission() -> None:
    async def run() -> None:
        h = Harness()
        request = event()
        h.fail_publish = True
        with pytest.raises(OSError):
            await h.receiver.receive(request)
        h.fail_publish = False
        await h.receiver.receive(event("user_input_result_requested", input_event_id=request["event_id"]))
        assert h.results[-1]["status"] == "accepted"
        assert len(h.submissions) == 1
    asyncio.run(run())


@pytest.mark.parametrize("fields", [
    {"session_id": str(uuid4())},
    {"speaker": {"role": "user", "participant_id": str(uuid4())}},
    {"speaker": {"role": "character", "participant_id": PARTICIPANT, "character_id": "miori"}},
    {"protocol_version": "1.0"},
    {"text": " \n\t"},
])
def test_invalid_input_never_reaches_submission(fields: dict[str, object]) -> None:
    async def run() -> None:
        h = Harness()
        with pytest.raises(TerminalProtocolError):
            await h.receiver.receive(event(**fields))
        assert h.submissions == []
        assert h.results == []
    asyncio.run(run())


def test_same_identifier_with_conflicting_payload_is_terminal() -> None:
    async def run() -> None:
        h = Harness()
        request = event()
        await h.receiver.receive(request)
        with pytest.raises(TerminalProtocolError, match="conflicting"):
            await h.receiver.receive({**request, "text": "別の入力"})
        assert len(h.submissions) == 1
    asyncio.run(run())


def test_unexpected_failure_cannot_be_reported_as_not_received() -> None:
    async def run() -> None:
        h = Harness()
        h.error = RuntimeError("private-text-sentinel")
        request = event()
        await h.receiver.receive(request)
        await h.receiver.receive(event("user_input_result_requested", input_event_id=request["event_id"]))
        assert h.results[-1]["status"] == "processing"
        assert h.results[-1]["error_code"] == "input_result_unknown"
        assert "private-text-sentinel" not in json.dumps(h.results)
        await h.receiver.receive(request)
        assert len(h.submissions) == 1
    asyncio.run(run())


def test_definite_rejection_remains_rejected_on_retry() -> None:
    async def run() -> None:
        h = Harness()
        h.error = TextInputRejected("input_invalid")
        request = event()
        await h.receiver.receive(request)
        await h.receiver.receive(request)
        assert [r["status"] for r in h.results] == ["rejected", "rejected"]
        assert len(h.submissions) == 1
    asyncio.run(run())


def test_capacity_never_evicts_receipts_or_missing_input_tombstones() -> None:
    async def run() -> None:
        h = Harness(max_receipts=1)
        request = event()
        await h.receiver.receive(event("user_input_result_requested", input_event_id=request["event_id"]))
        with pytest.raises(TerminalProtocolError, match="capacity"):
            await h.receiver.receive(event())
        await h.receiver.receive(request)
        assert h.results[-1]["status"] == "not_received"
        assert h.submissions == []
    asyncio.run(run())


def test_shared_text_input_fixture() -> None:
    root = Path(__file__).resolve().parents[3]
    fixture = json.loads((root / "contracts/voice-session/fixtures/text-input.json").read_text())
    for item in fixture["valid"]:
        parse_voice_session_event(item)
    for item in fixture["invalid"]:
        with pytest.raises(ValueError):
            parse_voice_session_event(item)
