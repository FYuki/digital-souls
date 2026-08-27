from __future__ import annotations

import importlib

import pytest


MODULE_NAME = "app.livekit_transport.outbox"


def _outbox_module(contract: str):
    try:
        return importlib.import_module(MODULE_NAME)
    except ModuleNotFoundError as error:
        if error.name is None or not (
            error.name == MODULE_NAME or MODULE_NAME.startswith(f"{error.name}.")
        ):
            raise
    pytest.fail(f"{MODULE_NAME} must implement {contract}")


def _payload(size: int) -> bytes:
    return b"x" * size


def test_outbox_accepts_exact_event_and_byte_limits_and_releases_on_ack() -> None:
    module = _outbox_module("bounded enqueue and ACK release")
    outbox = module.InMemoryOutbox(max_events=256, max_bytes=1_048_576)

    for index in range(256):
        outbox.enqueue(f"event-{index}", _payload(4096))

    assert outbox.event_count == 256
    assert outbox.byte_count == 1_048_576
    assert outbox.ack("event-0") is True
    assert outbox.event_count == 255
    assert outbox.byte_count == 1_044_480


@pytest.mark.parametrize(
    ("event_count", "event_size"),
    [(256, 1), (1, 1_048_576)],
)
def test_outbox_overflow_is_explicit_and_preserves_existing_events(
    event_count: int,
    event_size: int,
) -> None:
    module = _outbox_module("explicit capacity overflow")
    outbox = module.InMemoryOutbox(max_events=256, max_bytes=1_048_576)
    for index in range(event_count):
        outbox.enqueue(f"event-{index}", _payload(event_size))
    before = (outbox.event_count, outbox.byte_count)

    with pytest.raises(module.OutboxCapacityExceeded):
        outbox.enqueue("overflow", b"x")

    assert (outbox.event_count, outbox.byte_count) == before
    assert outbox.contains("overflow") is False
    assert outbox.transport_available is False


def test_outbox_clear_discards_all_session_scoped_delivery_state() -> None:
    module = _outbox_module("session-scoped delivery state cleanup")
    outbox = module.InMemoryOutbox(max_events=256, max_bytes=1_048_576)
    outbox.enqueue("event-1", b"payload")

    outbox.clear()

    assert outbox.event_count == 0
    assert outbox.byte_count == 0
    assert outbox.pending() == []


def test_outbox_capacity_ack_and_cleanup_are_isolated_by_session_and_direction() -> None:
    module = _outbox_module("session and sending-direction isolation")
    manager = module.InMemoryOutboxManager(max_events=256, max_bytes=1_048_576)
    session_a_outbound = manager.get("session-a", "backend-to-browser")
    session_a_inbound = manager.get("session-a", "browser-to-backend")
    session_b_outbound = manager.get("session-b", "backend-to-browser")

    for index in range(256):
        session_a_outbound.enqueue(f"session-a-event-{index}", _payload(4096))
    session_a_inbound.enqueue("shared-event", b"session-a-inbound")
    session_b_outbound.enqueue("shared-event", b"session-b-outbound")

    with pytest.raises(module.OutboxCapacityExceeded):
        session_a_outbound.enqueue("overflow", b"x")

    assert session_a_outbound.transport_available is False
    assert (session_a_inbound.event_count, session_a_inbound.byte_count) == (
        1,
        len(b"session-a-inbound"),
    )
    assert session_a_inbound.transport_available is True
    assert session_a_inbound.ack("shared-event") is True
    assert session_a_inbound.contains("shared-event") is False
    assert session_b_outbound.contains("shared-event") is True
    assert session_b_outbound.transport_available is True

    manager.clear_session("session-a")

    assert session_a_outbound.pending() == []
    assert session_a_inbound.pending() == []
    assert session_b_outbound.event_count == 1
    assert session_b_outbound.byte_count == len(b"session-b-outbound")
    assert session_b_outbound.contains("shared-event") is True
