from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import asyncio
import json
from uuid import uuid4

import pytest


SESSION_STARTED_PAYLOAD = (
    b'{"protocol_version":"1.0","event_id":'
    b'"10000000-0000-4000-8000-000000000010","type":"session_started",'
    b'"session_id":"20000000-0000-4000-8000-000000000010",'
    b'"monotonic_timestamp_ms":1000,"reconnect_grace_ms":60000}'
)


def _playback_payload(
    *, event_id: str, event_type: str = "playback_completed", sequence: int = 1
) -> bytes:
    event = {
        "protocol_version": "1.0",
        "event_id": event_id,
        "type": event_type,
        "session_id": "20000000-0000-4000-8000-000000000010",
        "response_id": "30000000-0000-4000-8000-000000000010",
        "last_played_audio_sequence": sequence,
        "monotonic_timestamp_ms": 1_000,
    }
    if event_type == "playback_stopped":
        event["reason"] = "disconnect"
    return json.dumps(event, separators=(",", ":")).encode()


@dataclass
class RecordingCorePort:
    notifications: list[bytes] = field(default_factory=list)

    def notify(self, payload: bytes) -> None:
        self.notifications.append(payload)


def _livekit_module(module_name: str, contract: str):
    qualified_name = f"app.livekit_transport.{module_name}"
    try:
        return importlib.import_module(qualified_name)
    except ModuleNotFoundError as error:
        if error.name is None or not (
            error.name == qualified_name
            or qualified_name.startswith(f"{error.name}.")
        ):
            raise
    pytest.fail(f"{qualified_name} must implement {contract}")


def test_core_event_deduplication_preserves_the_original_payload() -> None:
    module = _livekit_module("delivery", "payload-preserving event deduplication")
    deduplicator = module.EventDeduplicator()
    payload = SESSION_STARTED_PAYLOAD

    first = deduplicator.classify(
        "10000000-0000-4000-8000-000000000010", payload
    )
    duplicate = deduplicator.classify(
        "10000000-0000-4000-8000-000000000010", payload
    )

    assert first.status == "accepted"
    assert first.payload == payload
    assert duplicate.status == "duplicate"
    assert duplicate.payload == payload


def test_duplicate_core_event_is_not_reapplied_to_core_port() -> None:
    module = _livekit_module("delivery", "single delivery of duplicate Core events")
    core_port = RecordingCorePort()
    adapter = module.CoreEventDelivery(core_port=core_port)
    payload = SESSION_STARTED_PAYLOAD

    adapter.receive(payload)
    adapter.receive(payload)

    assert core_port.notifications == [payload]


def test_conflicting_duplicate_is_a_terminal_protocol_error() -> None:
    module = _livekit_module("delivery", "terminal conflicting-duplicate rejection")
    deduplicator = module.EventDeduplicator()
    event_id = "10000000-0000-4000-8000-000000000010"
    deduplicator.classify(event_id, b'{"value":1}')

    with pytest.raises(module.ConflictingDuplicateError):
        deduplicator.classify(event_id, b'{"value":2}')


def test_conflicting_duplicate_cleans_up_the_terminal_session() -> None:
    module = _livekit_module("delivery", "terminal-session cleanup after a conflict")
    cleanup_calls: list[str] = []
    adapter = module.CoreEventDelivery(
        core_port=RecordingCorePort(),
        cleanup=cleanup_calls.append,
    )
    first = SESSION_STARTED_PAYLOAD
    conflicting = first.replace(
        b'"reconnect_grace_ms":60000', b'"reconnect_grace_ms":30000'
    )
    adapter.receive(first)

    with pytest.raises(module.ConflictingDuplicateError):
        adapter.receive(conflicting)

    assert cleanup_calls == ["20000000-0000-4000-8000-000000000010"]


@pytest.mark.parametrize(
    "payload",
    [
        (
            b'{"protocol_version":"1.0","event_id":'
            b'"10000000-0000-4000-8000-000000000011","type":"unknown_event",'
            b'"session_id":"20000000-0000-4000-8000-000000000010",'
            b'"monotonic_timestamp_ms":1000}'
        ),
        (
            b'{"protocol_version":"1.0","event_id":'
            b'"10000000-0000-4000-8000-000000000012","type":"session_started",'
            b'"session_id":"20000000-0000-4000-8000-000000000010",'
            b'"monotonic_timestamp_ms":1000}'
        ),
    ],
    ids=("unknown-type", "invalid-payload"),
)
def test_invalid_core_event_is_terminal_and_cleans_up_session(payload: bytes) -> None:
    module = _livekit_module("delivery", "terminal invalid-event cleanup")
    cleanup_calls: list[str] = []
    core_port = RecordingCorePort()
    adapter = module.CoreEventDelivery(
        core_port=core_port,
        cleanup=cleanup_calls.append,
    )

    with pytest.raises(module.TerminalProtocolError):
        adapter.receive(payload)

    assert core_port.notifications == []
    assert cleanup_calls == ["20000000-0000-4000-8000-000000000010"]


def test_core_sequence_gap_is_terminal_before_core_notification() -> None:
    module = _livekit_module("delivery", "Core sequence gap detection")
    cleanup_calls: list[str] = []
    core_port = RecordingCorePort()
    adapter = module.CoreEventDelivery(
        core_port=core_port,
        cleanup=cleanup_calls.append,
    )
    payload = json.dumps(
        {
            "protocol_version": "1.0",
            "event_id": "10000000-0000-4000-8000-000000000013",
            "type": "response_delta",
            "session_id": "20000000-0000-4000-8000-000000000010",
            "response_id": "30000000-0000-4000-8000-000000000010",
            "text_sequence": 2,
            "text": "gap",
            "text_range": {"start": 0, "end": 3},
            "monotonic_timestamp_ms": 1000,
        },
        separators=(",", ":"),
    ).encode()

    with pytest.raises(module.SequenceGapError):
        adapter.receive(payload)

    assert core_port.notifications == []
    assert cleanup_calls == ["20000000-0000-4000-8000-000000000010"]


def test_retry_schedule_is_one_two_and_four_seconds_after_each_attempt() -> None:
    module = _livekit_module("delivery", "the one-two-four-second retry schedule")

    assert module.retry_deadlines_ms(10_000) == (11_000, 12_000, 14_000)


def test_three_unacknowledged_retries_make_transport_unavailable() -> None:
    module = _livekit_module("delivery", "transport failure after three unchanged retries")
    payload = b'{"event_id":"10000000-0000-4000-8000-000000000010"}'
    retry = module.RetryState(
        event_id="10000000-0000-4000-8000-000000000010",
        payload=payload,
        initially_sent_at_ms=10_000,
    )

    attempts = [retry.poll(now_ms) for now_ms in (11_000, 12_000, 14_000)]
    retry.poll(now_ms=17_001)

    assert [(attempt.event_id, attempt.payload) for attempt in attempts] == [
        ("10000000-0000-4000-8000-000000000010", payload),
        ("10000000-0000-4000-8000-000000000010", payload),
        ("10000000-0000-4000-8000-000000000010", payload),
    ]
    assert retry.transport_available is False
    assert retry.delivered is False


def test_reconnect_sync_contains_only_authoritative_state_and_terminal_outcomes() -> None:
    module = _livekit_module("delivery", "authoritative reconnect synchronization")

    frames = module.reconnect_sync_frames(
        authoritative_state={"phase": "available", "generation": 2},
        terminal_outcomes=[
            {
                "type": "response_interrupted",
                "session_id": "20000000-0000-4000-8000-000000000010",
                "response_id": "30000000-0000-4000-8000-000000000010",
                "confirmed_audio_sequence": 0,
            }
        ],
    )

    assert [frame["type"] for frame in frames] == ["authoritative_state"]
    assert frames[0]["terminal_outcomes"] == [
        {
            "type": "response_interrupted",
            "session_id": "20000000-0000-4000-8000-000000000010",
            "response_id": "30000000-0000-4000-8000-000000000010",
            "confirmed_audio_sequence": 0,
        }
    ]
    assert all("delta" not in frame and "audio" not in frame for frame in frames)


def test_disconnect_and_reconnect_keep_the_session_and_advance_generation() -> None:
    module = _livekit_module("lifecycle", "grace-period reconnection and generation advance")
    lifecycle = module.SessionLifecycle(
        session_id="20000000-0000-4000-8000-000000000010",
        reconnect_grace_ms=60_000,
    )
    lifecycle.activate()
    lifecycle.disconnect(now_ms=5_000)
    lifecycle.reconnect(now_ms=64_999)

    assert lifecycle.phase == "available"
    assert lifecycle.generation == 1
    assert lifecycle.session_ended is False
    assert lifecycle.accepts_generation(0) is False
    assert lifecycle.accepts_generation(1) is True


def test_reconnect_timeout_ends_the_session_and_clears_owned_state() -> None:
    module = _livekit_module("lifecycle", "reconnect-timeout cleanup")
    cleanup_calls: list[str] = []
    lifecycle = module.SessionLifecycle(
        session_id="20000000-0000-4000-8000-000000000010",
        reconnect_grace_ms=60_000,
        cleanup=cleanup_calls.append,
    )
    lifecycle.activate()
    lifecycle.disconnect(now_ms=5_000)

    ended = lifecycle.expire(now_ms=65_000)

    assert ended is True
    assert lifecycle.phase == "ended"
    assert cleanup_calls == ["room", "runtime", "outbox", "mapping"]


def test_explicit_end_is_idempotent() -> None:
    module = _livekit_module("lifecycle", "idempotent explicit termination")
    cleanup_calls: list[str] = []
    lifecycle = module.SessionLifecycle(
        session_id="20000000-0000-4000-8000-000000000010",
        reconnect_grace_ms=60_000,
        cleanup=cleanup_calls.append,
    )
    lifecycle.activate()

    first = lifecycle.end("explicit")
    second = lifecycle.end("explicit")

    assert first == second
    assert lifecycle.phase == "ended"
    assert cleanup_calls == ["cleanup"]


def test_never_joined_session_expires_at_the_join_token_deadline() -> None:
    module = _livekit_module("lifecycle", "never-joined token-deadline cleanup")
    cleanup_calls: list[str] = []
    lifecycle = module.SessionLifecycle(
        session_id="20000000-0000-4000-8000-000000000010",
        reconnect_grace_ms=60_000,
        cleanup=cleanup_calls.append,
        created_at_ms=5_000,
        join_deadline_ms=95_000,
    )

    assert lifecycle.expire_never_joined(now_ms=94_999) is False
    assert lifecycle.expire_never_joined(now_ms=95_000) is True
    assert lifecycle.phase == "ended"
    assert cleanup_calls == ["room", "runtime", "outbox", "mapping"]


def test_disconnect_discards_response_and_interrupts_at_confirmed_prefix_once() -> None:
    module = _livekit_module("lifecycle", "disconnect interruption at confirmed playback prefix")
    notifications: list[dict[str, object]] = []
    lifecycle = module.SessionLifecycle(
        session_id="20000000-0000-4000-8000-000000000010",
        reconnect_grace_ms=60_000,
        notify=notifications.append,
    )
    lifecycle.activate()
    lifecycle.begin_response(response_id="30000000-0000-4000-8000-000000000010")
    assert lifecycle.confirm_playback(
        response_id="30000000-0000-4000-8000-000000000011",
        confirmed_audio_sequence=3,
    ) is False
    assert lifecycle.confirm_playback(
        response_id="30000000-0000-4000-8000-000000000010",
        confirmed_audio_sequence=2,
    ) is True
    assert lifecycle.confirm_playback(
        response_id="30000000-0000-4000-8000-000000000010",
        confirmed_audio_sequence=1,
    ) is False

    lifecycle.disconnect(now_ms=5_000)
    lifecycle.disconnect(now_ms=6_000)

    assert lifecycle.in_progress_response is None
    assert notifications == [
        {
            "type": "response_interrupted",
            "session_id": "20000000-0000-4000-8000-000000000010",
            "response_id": "30000000-0000-4000-8000-000000000010",
            "confirmed_audio_sequence": 2,
        }
    ]


def _coordinator(module, published, cleaned, core_port=None):
    async def publish(payload: bytes, topic: str) -> None:
        published.append((payload, topic))

    async def cleanup(session_id: str) -> None:
        cleaned.append(session_id)

    async def generation_ready() -> None:
        return None

    return module.ProductionSessionCoordinator(
        session_id="20000000-0000-4000-8000-000000000010",
        user_identity="user-20000000-0000-4000-8000-000000000010",
        core_participant_id="40000000-0000-4000-8000-000000000010",
        reconnect_grace_ms=60_000,
        dependencies=module.SessionCoordinatorDependencies(
            publish_data=publish,
            cleanup=cleanup,
            generation_ready=generation_ready,
        ),
        core_port=core_port or RecordingCorePort(),
    )


def test_coordinator_releases_delivery_state_before_cleanup_dependency_finishes() -> None:
    module = _livekit_module("coordinator", "cleanup ownership release ordering")

    async def exercise() -> None:
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def publish(_payload: bytes, _topic: str) -> None:
            return None

        async def cleanup(_session_id: str) -> None:
            cleanup_started.set()
            await release_cleanup.wait()

        async def generation_ready() -> None:
            return None

        coordinator = module.ProductionSessionCoordinator(
            session_id="20000000-0000-4000-8000-000000000010",
            user_identity="user-20000000-0000-4000-8000-000000000010",
            core_participant_id="40000000-0000-4000-8000-000000000010",
            reconnect_grace_ms=60_000,
            dependencies=module.SessionCoordinatorDependencies(
                publish_data=publish,
                cleanup=cleanup,
                generation_ready=generation_ready,
            ),
            core_port=RecordingCorePort(),
        )
        identity = "user-20000000-0000-4000-8000-000000000010"
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_current", room_sid="RM_one"
        )
        await coordinator.send_core(SESSION_STARTED_PAYLOAD)

        cleanup_task = asyncio.create_task(coordinator.cleanup("explicit"))
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)

        assert coordinator.phase == "ended"
        assert not coordinator.is_current_participant(
            identity=identity, participant_sid="PA_current"
        )
        assert coordinator.acknowledge(
            "10000000-0000-4000-8000-000000000010", "character_to_user"
        ) is False

        release_cleanup.set()
        await asyncio.wait_for(cleanup_task, timeout=0.5)

    asyncio.run(exercise())


def test_disconnect_resynchronizes_the_acknowledged_terminal_outcome() -> None:
    module = _livekit_module("coordinator", "terminal outcome reconnection sync")

    async def exercise() -> None:
        published: list[tuple[bytes, str]] = []
        core_port = RecordingCorePort()
        coordinator = _coordinator(module, published, [], core_port)
        identity = "user-20000000-0000-4000-8000-000000000010"
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_initial", room_sid="RM_one"
        )
        coordinator.begin_response(
            response_id="30000000-0000-4000-8000-000000000010"
        )
        playback = _playback_payload(
            event_id="10000000-0000-4000-8000-000000000020"
        )
        await coordinator.receive_data(
            identity=identity,
            participant_sid="PA_initial",
            topic=module.APPLICATION_TOPIC,
            payload=playback,
        )

        coordinator.participant_disconnected(
            identity=identity, participant_sid="PA_initial"
        )
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_reconnected", room_sid="RM_one"
        )
        await coordinator.synchronize_reconnection()

        private_frames = [
            json.loads(payload)
            for payload, topic in published
            if topic == module.PRIVATE_TOPIC
        ]
        assert private_frames[-1] == {
            "protocol_version": "1.0",
            "type": "authoritative_state",
            "generation": 1,
            "session_phase": "available",
            "terminal_outcomes": [
                {
                    "type": "response_interrupted",
                    "session_id": "20000000-0000-4000-8000-000000000010",
                    "response_id": "30000000-0000-4000-8000-000000000010",
                    "confirmed_audio_sequence": 1,
                }
            ],
        }
        assert core_port.notifications[0] == playback
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())


def test_state_sync_request_interrupts_once_before_authoritative_state() -> None:
    module = _livekit_module("coordinator", "generation-owned terminal outcome sync")

    async def exercise() -> None:
        published: list[tuple[bytes, str]] = []
        coordinator = _coordinator(module, published, [])
        identity = "user-20000000-0000-4000-8000-000000000010"
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_current", room_sid="RM_one"
        )
        coordinator.begin_response(
            response_id="30000000-0000-4000-8000-000000000010"
        )
        await coordinator.receive_data(
            identity=identity,
            participant_sid="PA_current",
            topic=module.APPLICATION_TOPIC,
            payload=_playback_payload(
                event_id="10000000-0000-4000-8000-000000000021",
                event_type="playback_stopped",
            ),
        )
        sync_request = json.dumps(
            {
                "protocol_version": "1.0",
                "type": "state_sync_request",
                "generation": 0,
            },
            separators=(",", ":"),
        ).encode()

        await coordinator.receive_data(
            identity=identity,
            participant_sid="PA_current",
            topic=module.PRIVATE_TOPIC,
            payload=sync_request,
        )

        authoritative = json.loads(published[-1][0])
        assert authoritative["generation"] == 1
        assert authoritative["terminal_outcomes"] == [
            {
                "type": "response_interrupted",
                "session_id": "20000000-0000-4000-8000-000000000010",
                "response_id": "30000000-0000-4000-8000-000000000010",
                "confirmed_audio_sequence": 1,
            }
        ]
        assert all(
            frame.get("type") != "terminal_outcome"
            for frame in (json.loads(payload) for payload, _topic in published)
        )
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())


def test_production_data_handler_acknowledges_and_stops_retry() -> None:
    module = _livekit_module("coordinator", "production ACK and outbox ownership")

    async def exercise() -> None:
        published: list[tuple[bytes, str]] = []
        coordinator = _coordinator(module, published, [])
        identity = "user-20000000-0000-4000-8000-000000000010"
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_current", room_sid="RM_one"
        )
        await coordinator.send_core(SESSION_STARTED_PAYLOAD)
        event_id = "10000000-0000-4000-8000-000000000010"
        ack = json.dumps(
            {
                "protocol_version": "1.0",
                "type": "ack",
                "event_id": event_id,
                "generation": 0,
            }
        ).encode()

        await coordinator.receive_data(
            identity=identity,
            participant_sid="PA_current",
            topic=module.PRIVATE_TOPIC,
            payload=ack,
        )
        await asyncio.sleep(0)

        assert coordinator.acknowledge(event_id, "character_to_user") is False
        assert published == [(SESSION_STARTED_PAYLOAD, module.APPLICATION_TOPIC)]
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())


def test_outbound_duplicate_is_sent_once_and_conflicting_payload_ends_session() -> None:
    module = _livekit_module("coordinator", "outbound event deduplication")

    async def exercise() -> None:
        published: list[tuple[bytes, str]] = []
        cleaned: list[str] = []
        coordinator = _coordinator(module, published, cleaned)
        coordinator.participant_connected(
            identity="user-20000000-0000-4000-8000-000000000010",
            participant_sid="PA_current",
            room_sid="RM_one",
        )
        await coordinator.send_core(SESSION_STARTED_PAYLOAD)
        await coordinator.send_core(SESSION_STARTED_PAYLOAD)
        conflicting = SESSION_STARTED_PAYLOAD.replace(
            b'"monotonic_timestamp_ms":1000', b'"monotonic_timestamp_ms":1001'
        )

        assert published == [(SESSION_STARTED_PAYLOAD, module.APPLICATION_TOPIC)]
        with pytest.raises(module.TerminalProtocolError):
            await coordinator.send_core(conflicting)
        assert coordinator.phase == "ended"
        assert cleaned == ["20000000-0000-4000-8000-000000000010"]

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["bootstrapping", "unavailable"])
def test_send_core_rejects_inactive_phase_without_delivery_state(
    phase: str,
) -> None:
    module = _livekit_module("coordinator", "available-only control delivery")

    async def exercise() -> None:
        published: list[tuple[bytes, str]] = []
        coordinator = _coordinator(module, published, [])
        if phase == "unavailable":
            identity = "user-20000000-0000-4000-8000-000000000010"
            coordinator.participant_connected(
                identity=identity,
                participant_sid="PA_current",
                room_sid="RM_one",
            )
            coordinator.participant_disconnected(
                identity=identity, participant_sid="PA_current"
            )

        with pytest.raises(RuntimeError, match="session is not available"):
            await coordinator.send_core(SESSION_STARTED_PAYLOAD)

        assert coordinator.phase == phase
        assert published == []
        assert coordinator._retry_tasks == {}
        assert coordinator.acknowledge(
            "10000000-0000-4000-8000-000000000010", "character_to_user"
        ) is False
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())


def test_three_identical_retries_end_unavailable_session(monkeypatch) -> None:
    module = _livekit_module("coordinator", "production retry exhaustion")
    original_sleep = asyncio.sleep

    async def controlled_sleep(seconds: float) -> None:
        if seconds >= 60:
            await asyncio.Future()
        await original_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", controlled_sleep)

    async def exercise() -> None:
        published: list[tuple[bytes, str]] = []
        cleaned: list[str] = []
        coordinator = _coordinator(module, published, cleaned)
        coordinator.participant_connected(
            identity="user-20000000-0000-4000-8000-000000000010",
            participant_sid="PA_current",
            room_sid="RM_one",
        )
        await coordinator.send_core(SESSION_STARTED_PAYLOAD)
        for _ in range(8):
            await original_sleep(0)

        application_payloads = [
            payload for payload, topic in published if topic == module.APPLICATION_TOPIC
        ]
        assert application_payloads == [SESSION_STARTED_PAYLOAD] * 4
        assert coordinator.phase == "unavailable"
        assert cleaned == []
        assert coordinator.acknowledge(
            "10000000-0000-4000-8000-000000000010", "character_to_user"
        ) is True
        await coordinator.cleanup("test_complete")
        assert cleaned == ["20000000-0000-4000-8000-000000000010"]

    asyncio.run(exercise())


def test_ack_after_first_retry_stops_remaining_retries(monkeypatch) -> None:
    module = _livekit_module("coordinator", "ACK-controlled retry termination")
    original_sleep = asyncio.sleep
    retry_waiting = asyncio.Event()
    release_retry = asyncio.Event()
    retry_sleep_count = 0

    async def controlled_sleep(seconds: float) -> None:
        nonlocal retry_sleep_count
        if seconds >= 60:
            await asyncio.Future()
        retry_sleep_count += 1
        if retry_sleep_count == 1:
            retry_waiting.set()
            await release_retry.wait()
            return
        await asyncio.Future()

    monkeypatch.setattr(module.asyncio, "sleep", controlled_sleep)

    async def exercise() -> None:
        published: list[tuple[bytes, str]] = []
        coordinator = _coordinator(module, published, [])
        identity = "user-20000000-0000-4000-8000-000000000010"
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_current", room_sid="RM_one"
        )
        await coordinator.send_core(SESSION_STARTED_PAYLOAD)
        await retry_waiting.wait()
        release_retry.set()
        while len(published) < 2:
            await original_sleep(0)

        ack = json.dumps(
            {
                "protocol_version": "1.0",
                "type": "ack",
                "event_id": "10000000-0000-4000-8000-000000000010",
                "generation": 0,
            }
        ).encode()
        await coordinator.receive_data(
            identity=identity,
            participant_sid="PA_current",
            topic=module.PRIVATE_TOPIC,
            payload=ack,
        )
        await original_sleep(0)

        assert published == [
            (SESSION_STARTED_PAYLOAD, module.APPLICATION_TOPIC),
            (SESSION_STARTED_PAYLOAD, module.APPLICATION_TOPIC),
        ]
        assert coordinator.phase == "available"
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())


def test_duplicate_join_replaces_current_sid_and_advances_on_real_rejoin() -> None:
    module = _livekit_module("coordinator", "participant-owned generation lifecycle")

    async def exercise() -> None:
        core_port = RecordingCorePort()
        coordinator = _coordinator(module, [], [], core_port)
        identity = "user-20000000-0000-4000-8000-000000000010"
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_old", room_sid="RM_one"
        )
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_new", room_sid="RM_one"
        )
        coordinator.participant_disconnected(
            identity=identity, participant_sid="PA_old"
        )

        assert coordinator.is_current_participant(
            identity=identity, participant_sid="PA_new"
        )
        assert not coordinator.is_current_participant(
            identity=identity, participant_sid="PA_old"
        )
        assert coordinator.phase == "available"
        assert coordinator.generation == 0

        coordinator.participant_disconnected(
            identity=identity, participant_sid="PA_new"
        )
        reconnected = coordinator.participant_connected(
            identity=identity, participant_sid="PA_rejoin", room_sid="RM_one"
        )
        assert reconnected is True
        await coordinator.synchronize_reconnection()
        assert coordinator.phase == "available"
        assert coordinator.generation == 1
        assert [
            json.loads(payload)["type"] for payload in core_port.notifications
        ] == ["session_disconnected", "session_reconnected"]
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())


def test_old_sid_data_is_ignored_after_duplicate_join() -> None:
    module = _livekit_module("coordinator", "current-participant data ownership")

    async def exercise() -> None:
        published: list[tuple[bytes, str]] = []
        core_port = RecordingCorePort()
        coordinator = _coordinator(module, published, [], core_port)
        identity = "user-20000000-0000-4000-8000-000000000010"
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_old", room_sid="RM_one"
        )
        coordinator.participant_connected(
            identity=identity, participant_sid="PA_new", room_sid="RM_one"
        )

        await coordinator.receive_data(
            identity=identity,
            participant_sid="PA_old",
            topic=module.APPLICATION_TOPIC,
            payload=SESSION_STARTED_PAYLOAD,
        )

        assert core_port.notifications == []
        assert published == []

        await coordinator.receive_data(
            identity=identity,
            participant_sid="PA_new",
            topic=module.APPLICATION_TOPIC,
            payload=SESSION_STARTED_PAYLOAD,
        )

        assert core_port.notifications == [SESSION_STARTED_PAYLOAD]
        assert len(published) == 1
        assert published[0][1] == module.PRIVATE_TOPIC
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())


def test_production_outbox_overflow_keeps_resources_and_pending_events() -> None:
    module = _livekit_module("coordinator", "production outbox overflow transition")

    async def exercise() -> None:
        cleaned: list[str] = []
        coordinator = _coordinator(module, [], cleaned)
        coordinator.participant_connected(
            identity="user-20000000-0000-4000-8000-000000000010",
            participant_sid="PA_current",
            room_sid="RM_one",
        )
        for _ in range(256):
            payload = SESSION_STARTED_PAYLOAD.replace(
                b"10000000-0000-4000-8000-000000000010",
                str(uuid4()).encode(),
                1,
            )
            await coordinator.send_core(payload)
        overflow = SESSION_STARTED_PAYLOAD.replace(
            b"10000000-0000-4000-8000-000000000010",
            str(uuid4()).encode(),
            1,
        )

        with pytest.raises(module.OutboxCapacityExceeded):
            await coordinator.send_core(overflow)

        assert coordinator.phase == "unavailable"
        assert cleaned == []
        await coordinator.cleanup("test_complete")
        assert cleaned == ["20000000-0000-4000-8000-000000000010"]

    asyncio.run(exercise())


def test_production_never_joined_deadline_owns_cleanup(monkeypatch) -> None:
    module = _livekit_module("coordinator", "production never-joined cleanup")
    original_sleep = asyncio.sleep

    async def immediate_sleep(_seconds: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", immediate_sleep)

    async def exercise() -> None:
        cleaned: list[str] = []
        coordinator = _coordinator(module, [], cleaned)
        coordinator.start_join_deadline()
        await original_sleep(0)
        await original_sleep(0)

        assert coordinator.phase == "ended"
        assert cleaned == ["20000000-0000-4000-8000-000000000010"]

    asyncio.run(exercise())
